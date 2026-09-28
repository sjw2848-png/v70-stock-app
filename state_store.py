import json
import os
import threading
import time
from datetime import datetime, timezone


class StorageUnavailable(OSError):
    """Raised when the authoritative store (PostgreSQL) cannot be read.

    V78.11.0: previously a failed DB read silently fell back to the ephemeral local mirror
    (often empty on Render).  A following write then pushed that partial dict back to the
    database and could overwrite every other account/portfolio.  Reads now fail loudly so
    callers return HTTP 503 and nothing is written from a stale snapshot.
    """


class StateStore:
    """Small JSON state store with PostgreSQL primary storage and atomic-file fallback.

    If DATABASE_URL is configured, reads and writes MUST reach PostgreSQL; the local mirror is
    only a same-instance convenience backup and is never treated as authoritative.  Without a
    database, atomic JSON files are used and the caller should surface that persistence across
    deploys depends on the hosting platform (e.g. an attached persistent disk).
    """

    def __init__(self, data_dir, database_url=""):
        self.data_dir = os.path.abspath(data_dir)
        self.database_url = str(database_url or "").strip()
        self._db_ready = False
        self._db_lock = threading.RLock()
        self._conn = None
        self.last_error = ""
        self._last_status_attempt = 0.0
        self.stats = {"db_connects": 0, "db_reads": 0, "db_writes": 0, "db_retries": 0}
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError:
            pass

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ connection reuse
    def _connect(self):
        import psycopg
        conn = psycopg.connect(self.database_url, connect_timeout=6, autocommit=True,
                               application_name='v78-stock-app')
        self.stats["db_connects"] += 1
        return conn

    def _get_conn(self):
        conn = self._conn
        if conn is None or conn.closed:
            self._conn = conn = self._connect()
        return conn

    def _drop_conn(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _run(self, fn):
        """Run fn(cursor) on the shared connection; reconnect and retry once on failure.

        Opening a fresh TLS connection for every read/write (the previous behaviour) cost
        50~200 ms each and a portfolio save did 4~6 of them.  One autocommit connection guarded
        by a lock is plenty for a single-worker service.
        """
        with self._db_lock:
            for attempt in (1, 2):
                try:
                    conn = self._get_conn()
                    with conn.cursor() as cur:
                        return fn(cur)
                except Exception:
                    self._drop_conn()
                    if attempt == 2:
                        raise
                    self.stats["db_retries"] += 1

    def _ensure_db(self):
        if not self.database_url or self._db_ready:
            return
        with self._db_lock:
            if self._db_ready:
                return
            self._run(lambda cur: cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_app_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """))
            self._db_ready = True
            self.last_error = ""

    def _db_read(self, key):
        self._ensure_db()

        def op(cur):
            cur.execute("SELECT payload FROM stock_app_state WHERE state_key=%s", (key,))
            return cur.fetchone()
        row = self._run(op)
        self.stats["db_reads"] += 1
        self.last_error = ""
        if not row:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def _db_write(self, key, value):
        self._ensure_db()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self._run(lambda cur: cur.execute(
            """
            INSERT INTO stock_app_state(state_key, payload, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT(state_key)
            DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()
            """, (key, payload)))
        self.stats["db_writes"] += 1
        self.last_error = ""

    # ------------------------------------------------------------------ files
    @staticmethod
    def _file_read(path):
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = json.load(f)
            return value if isinstance(value, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _file_write_atomic(path, value, backup_path=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if backup_path and os.path.exists(path):
            previous = StateStore._file_read(path)
            if isinstance(previous, dict):
                btmp = backup_path + ".tmp"
                with open(btmp, "w", encoding="utf-8") as bf:
                    json.dump(previous, bf, ensure_ascii=False, separators=(",", ":"))
                    bf.flush(); os.fsync(bf.fileno())
                os.replace(btmp, backup_path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------------ public API
    def read(self, key, file_path, backup_path=None):
        if self.database_url:
            try:
                value = self._db_read(key)
            except Exception as exc:
                self.last_error = str(exc)[:220]
                raise StorageUnavailable(f"PostgreSQL 읽기 실패: {str(exc)[:160]}") from exc
            if isinstance(value, dict):
                return value
            # First run against an empty DB: import a legacy file copy once.
            legacy = self._file_read(file_path) or self._file_read(backup_path)
            if isinstance(legacy, dict) and legacy:
                try:
                    self._db_write(key, legacy)
                except Exception as exc:
                    self.last_error = str(exc)[:220]
                    raise StorageUnavailable(f"PostgreSQL 초기 이관 실패: {str(exc)[:160]}") from exc
                return legacy
            return {}
        value = self._file_read(file_path)
        if isinstance(value, dict):
            return value
        backup = self._file_read(backup_path)
        return backup if isinstance(backup, dict) else {}

    def write(self, key, value, file_path, backup_path=None):
        if not isinstance(value, dict):
            raise ValueError("state value must be a dict")
        db_error = None
        if self.database_url:
            try:
                self._db_write(key, value)
            except Exception as exc:
                self.last_error = str(exc)[:220]
                db_error = exc
        # Local mirror: useful for same-instance recovery even when PostgreSQL is primary.
        file_error = None
        try:
            self._file_write_atomic(file_path, value, backup_path)
        except Exception as exc:
            file_error = exc
        if self.database_url and db_error is not None:
            raise OSError(f"PostgreSQL 저장 실패: {str(db_error)[:160]}")
        if not self.database_url and file_error is not None:
            raise OSError(f"파일 저장 실패: {str(file_error)[:160]}")

    def probe_database(self):
        """Perform a real round-trip write/read test without touching user portfolio data."""
        if not self.database_url:
            return {"ok": False, "reason": "not_configured"}
        token = self._now()
        try:
            self._ensure_db()

            def op(cur):
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stock_app_storage_probe (
                        probe_key TEXT PRIMARY KEY,
                        probe_value TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """)
                cur.execute(
                    """INSERT INTO stock_app_storage_probe(probe_key, probe_value, updated_at)
                       VALUES ('primary', %s, NOW())
                       ON CONFLICT(probe_key) DO UPDATE
                       SET probe_value=EXCLUDED.probe_value, updated_at=NOW()""",
                    (token,))
                cur.execute("SELECT probe_value, updated_at FROM stock_app_storage_probe WHERE probe_key='primary'")
                return cur.fetchone()
            row = self._run(op)
            ok = bool(row and str(row[0]) == token)
            if ok:
                self.last_error = ""
            return {"ok": ok, "written": token, "read_back": str(row[0]) if row else None,
                    "checked_at": self._now()}
        except Exception as exc:
            self.last_error = str(exc)[:220]
            return {"ok": False, "error": self.last_error, "checked_at": self._now()}

    def status(self):
        mounted = False
        try:
            mounted = os.path.ismount(self.data_dir)
        except Exception:
            mounted = False
        # Retry DB initialisation at most once a minute so /health stays fast while the DB is down.
        if self.database_url and not self._db_ready and time.time() - self._last_status_attempt > 60:
            self._last_status_attempt = time.time()
            try:
                self._ensure_db()
            except Exception as exc:
                self.last_error = str(exc)[:220]
        if self.database_url and self._db_ready and not self.last_error:
            durability = "database"
        elif mounted:
            durability = "persistent-disk"
        else:
            durability = "ephemeral-or-unknown"
        return {
            "mode": "postgres" if self.database_url else "file",
            "database_configured": bool(self.database_url),
            "database_ok": bool(self.database_url and self._db_ready and not self.last_error),
            "file_mount_detected": mounted,
            "data_dir": self.data_dir,
            "last_error": self.last_error,
            "durability": durability,
            "checked_at": self._now(),
        }
