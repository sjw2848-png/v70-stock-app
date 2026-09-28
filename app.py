import hashlib
import hmac
import secrets
import atexit
import json
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, request, g
import re
import requests
from lxml import html as lxml_html
from state_store import StateStore, StorageUnavailable
from engine import analyze, analyze_search, search_instruments, fetch_fundamentals, fetch_recent_issues
import market_calendar

APP_VERSION = 'V78.12.1'
app = Flask(__name__)

_cache_lock = threading.Lock()
_cache = {}
_last_request_by_ip = {}
_fund_cache = {}
_issue_cache = {}
_surge_cache = {}
_search_cache = {}
_extra_cache_lock = threading.Lock()
CACHE_TTL = max(0, int(os.environ.get('CACHE_TTL_SECONDS', '120')))
SEARCH_CACHE_TTL = max(0, int(os.environ.get('SEARCH_CACHE_TTL_SECONDS', '90')))
MIN_REQUEST_GAP = max(0.0, float(os.environ.get('MIN_REQUEST_GAP_SECONDS', '2')))
APP_PIN = os.environ.get('APP_PIN', '').strip()

# Cross-device portfolio/watchlist sync. Point DATA_DIR at a persistent disk in production.
_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DATA_DIR = os.environ.get('DATA_DIR', _DEFAULT_DATA_DIR)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _probe = os.path.join(DATA_DIR, '.write_probe')
    with open(_probe, 'w', encoding='utf-8') as _f:
        _f.write('ok')
    os.remove(_probe)
except OSError:
    # V78.12.0: render.yaml sets DATA_DIR=/var/data, but the free plan has no disk and the
    # directory may not be writable.  Crashing at import would take the whole app down, so fall
    # back to the app folder (PostgreSQL remains the durable store).
    DATA_DIR = _DEFAULT_DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
PORTFOLIO_BACKUP_FILE = os.path.join(DATA_DIR, 'portfolio.backup.json')
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')
ACCOUNTS_BACKUP_FILE = os.path.join(DATA_DIR, 'accounts.backup.json')
# V78.9.0: database-first persistence. DATABASE_URL is the standard Render variable.
# STOCK_DATABASE_URL / LOTTO_DATABASE_URL are accepted as explicit aliases so the stock
# service can reuse an already-managed PostgreSQL instance without changing application code.
_DATABASE_CANDIDATES = [
    ('DATABASE_URL', os.environ.get('DATABASE_URL', '')),
    ('STOCK_DATABASE_URL', os.environ.get('STOCK_DATABASE_URL', '')),
    ('LOTTO_DATABASE_URL', os.environ.get('LOTTO_DATABASE_URL', '')),
]
DATABASE_SOURCE, DATABASE_URL = next(((k, str(v or '').strip()) for k, v in _DATABASE_CANDIDATES if str(v or '').strip()), ('', ''))
_state_store = StateStore(DATA_DIR, DATABASE_URL)
_portfolio_lock = threading.Lock()
_account_lock = threading.Lock()
ACCOUNT_COOKIE = 'v7863_account_session'
ACCOUNT_SESSION_DAYS = max(1, min(90, int(os.environ.get('ACCOUNT_SESSION_DAYS', '30'))))

def _session_token_hash(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()

def _prune_sessions(rec):
    now = int(time.time())
    sessions = rec.get('sessions', []) if isinstance(rec, dict) else []
    if not isinstance(sessions, list):
        sessions = []
    clean = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        if int(item.get('expires_at') or 0) > now and item.get('token_hash'):
            clean.append(item)
    rec['sessions'] = clean[-12:]
    return rec

def _issue_account_session(account_id):
    token = secrets.token_urlsafe(32)
    token_hash = _session_token_hash(token)
    expires_at = int(time.time()) + ACCOUNT_SESSION_DAYS * 86400
    with _account_lock:
        accounts = _read_accounts()
        rec = accounts.get(account_id)
        if not isinstance(rec, dict):
            return None
        rec = _prune_sessions(dict(rec))
        rec['sessions'].append({'token_hash': token_hash, 'expires_at': expires_at, 'created_at': _now_iso()})
        rec['sessions'] = rec['sessions'][-12:]
        accounts[account_id] = rec
        _write_accounts(accounts)
    return f'{account_id}.{token}', expires_at

def _request_is_https():
    proto = str(request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    return bool(request.is_secure or proto == 'https')

def _set_account_cookie(response, cookie_value, expires_at=None):
    if not cookie_value:
        return response
    response.set_cookie(ACCOUNT_COOKIE, cookie_value, max_age=ACCOUNT_SESSION_DAYS*86400,
                        httponly=True, samesite='Lax', secure=_request_is_https(), path='/')
    return response

def _clear_account_cookie(response):
    response.delete_cookie(ACCOUNT_COOKIE, path='/', samesite='Lax', secure=_request_is_https())
    return response

def _normalize_account_id(value):
    raw = str(value or '').strip().lower()
    return ''.join(ch for ch in raw if ch.isalnum() or ch in {'-','_','.'})[:40]

def _read_accounts():
    # V78.9.0: account registry and portfolio now share the same persistence abstraction.
    # The old code accidentally used portfolio.backup.json as an account fallback, which could
    # make a valid ID appear to be missing after a damaged/missing accounts.json.
    return _state_store.read('accounts', ACCOUNTS_FILE, ACCOUNTS_BACKUP_FILE)

def _write_accounts(data):
    _state_store.write('accounts', data, ACCOUNTS_FILE, ACCOUNTS_BACKUP_FILE)

def _password_hash(password, salt_hex, iterations=180000):
    return hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), bytes.fromhex(salt_hex), iterations).hex()

_login_failures = {}
_LOGIN_WINDOW = 600
_LOGIN_MAX_FAILURES = 8
PASSWORD_ITERATIONS = 210000


def _client_ip():
    return str(request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')).split(',')[0].strip()


def _login_throttled(account_id):
    key = (_client_ip(), account_id)
    now = time.time()
    hits = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW]
    _login_failures[key] = hits
    return len(hits) >= _LOGIN_MAX_FAILURES


def _record_login_failure(account_id):
    key = (_client_ip(), account_id)
    now = time.time()
    _login_failures.setdefault(key, []).append(now)
    if len(_login_failures) > 2000:
        for k in [k for k, v in _login_failures.items() if not v or now - v[-1] > _LOGIN_WINDOW]:
            _login_failures.pop(k, None)


def _owner_for(account_id):
    return hashlib.sha256(('account:' + account_id).encode('utf-8')).hexdigest()[:32]


def _cookie_session_account():
    """Return the account id of a valid HttpOnly session cookie, or ''.

    V78.11.0: the registry is only rewritten when expired sessions were actually pruned.
    The old code wrote the whole account registry to PostgreSQL on *every* authenticated
    request (each portfolio GET/PUT), which was slow and widened the race window.
    """
    cookie = str(request.cookies.get(ACCOUNT_COOKIE) or '')
    if '.' not in cookie:
        return ''
    cookie_id, token = cookie.split('.', 1)
    account_id = _normalize_account_id(cookie_id)
    token_hash = _session_token_hash(token)
    with _account_lock:
        accounts = _read_accounts()
        rec = accounts.get(account_id)
        if not isinstance(rec, dict):
            return ''
        before = len(rec.get('sessions', []) or [])
        pruned = _prune_sessions(dict(rec))
        valid = any(hmac.compare_digest(str(x.get('token_hash')), token_hash) for x in pruned.get('sessions', []))
        if len(pruned.get('sessions', [])) != before:
            accounts[account_id] = pruned
            _write_accounts(accounts)
    return account_id if valid else ''


def _account_auth():
    header_account_id = _normalize_account_id(request.headers.get('X-Account-ID'))
    header_password = str(request.headers.get('X-Account-Password') or '')
    # 1) Prefer the persistent HttpOnly cookie when it belongs to the requested account (or no
    #    account was named).  Previously any password header bypassed the cookie, so every
    #    request re-ran 180k PBKDF2 rounds.
    cookie_account = _cookie_session_account()
    if cookie_account and (not header_account_id or header_account_id == cookie_account):
        g.account_id = cookie_account
        g.account_auth_via = 'cookie'
        return _owner_for(cookie_account), None

    # 2) Explicit ID/password for first login, another device, or deliberate account switch.
    account_id = header_account_id
    password = header_password
    if not account_id or not password:
        return None, '개인 계정 연결이 필요합니다.'
    if _login_throttled(account_id):
        return None, '비밀번호 오류가 반복되어 10분간 로그인이 제한됩니다.'
    with _account_lock:
        accounts = _read_accounts()
        rec = accounts.get(account_id)
    if not isinstance(rec, dict):
        return None, '등록되지 않은 개인 계정입니다.'
    salt = str(rec.get('salt') or '')
    expected = str(rec.get('password_hash') or '')
    try:
        actual = _password_hash(password, salt, int(rec.get('iterations') or 180000))
    except Exception:
        return None, '개인 계정 인증정보가 손상되었습니다.'
    if not hmac.compare_digest(actual, expected):
        _record_login_failure(account_id)
        return None, '개인 계정 비밀번호가 올바르지 않습니다.'
    # A valid login resets this IP/account failure bucket instead of carrying old failures
    # forward until the full 10-minute window expires.
    _login_failures.pop((_client_ip(), account_id), None)
    g.account_id = account_id
    g.account_auth_via = 'password'
    return _owner_for(account_id), None

def _portfolio_owner_legacy():
    # V78.5 이하의 기존 동기화 공간. 신규 개인계정으로 이관할 때만 사용합니다.
    sync_key = (request.headers.get('X-Sync-Key') or '').strip()
    seed = ('sync:' + sync_key) if sync_key else ('legacy:' + (APP_PIN or 'local-default'))
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]

def _read_portfolio_store():
    return _state_store.read('portfolios', PORTFOLIO_FILE, PORTFOLIO_BACKUP_FILE)

def _write_portfolio_store(data):
    _state_store.write('portfolios', data, PORTFOLIO_FILE, PORTFOLIO_BACKUP_FILE)

def _storage_status():
    st = _state_store.status()
    # /var/data is only truly durable when the host has an attached persistent disk. A path
    # name alone is not proof, so report it as host-filesystem unless PostgreSQL is configured.
    st['configured_data_dir'] = os.path.abspath(DATA_DIR)
    st['render_free_disk_warning'] = (not DATABASE_URL and os.environ.get('RENDER','').lower() in {'1','true','yes'})
    st['database_source'] = DATABASE_SOURCE or None
    st['database_namespace'] = 'stock_app_state'
    return st

def _portfolio_digest(rows):
    payload = json.dumps(rows if isinstance(rows, list) else [], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


# Market clocks are calculated on the server with explicit time zones (see market_calendar.py).
def _market_session(market):
    return market_calendar.market_session(market)


def _market_sessions():
    return market_calendar.market_sessions()


PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.v76.pid')

def _cleanup_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass

atexit.register(_cleanup_pid)


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _payload_hash(settings):
    raw = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _authorized():
    if not APP_PIN:
        return True
    supplied = (request.headers.get('X-App-Pin') or '').strip()
    return hmac.compare_digest(supplied.encode('utf-8'), APP_PIN.encode('utf-8'))


@app.errorhandler(StorageUnavailable)
def _storage_unavailable(exc):
    return jsonify({'ok': False, 'error': f'서버 저장소(DB)에 일시적으로 접근할 수 없습니다. 기기 자료는 보존됩니다. ({str(exc)[:120]})',
                    'code': 'STORAGE_UNAVAILABLE', 'version': APP_VERSION}), 503


@app.get('/')
def home():
    return render_template('index.html', version=APP_VERSION, pin_required=bool(APP_PIN))


@app.after_request
def _shell_cache_headers(response):
    # V78.12.1: PWA shell must revalidate on every deploy so old app.js does not linger.
    if request.path in ('/', '/static/app.js', '/static/sw.js', '/static/manifest.json'):
        response.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.get('/health')
def health():
    return jsonify({
        'ok': True,
        'version': APP_VERSION,
        'time': _now_iso(),
        'pin_required': bool(APP_PIN),
        'cache_ttl_seconds': CACHE_TTL,
        'sessions': _market_sessions(),
        'holidays': market_calendar.holiday_payload(),
        'storage': _storage_status(),
    })


def _validated_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError('요청 형식이 올바르지 않습니다.')
    defaults = {
        'budget': 5_000_000, 'trade_budget': 300_000, 'long_budget': 1_000_000,
        'risk_pct': 1.0, 'stop_pct': 3.0, 'min_rrr': 1.5,
        'trust_mode': 'balanced', 'mode': 'smart', 'top_n': 60, 'held': '',
        'search_avg_price': 0, 'search_held_qty': 0, 'saving_goal_krw': 1_000_000, 'monthly_saving_krw': 100_000,
    }
    out = {**defaults, **payload}
    def num(key, lo, hi):
        try: v = float(out.get(key, defaults[key]))
        except (TypeError, ValueError): raise ValueError(f'{key} 값이 숫자가 아닙니다.')
        if not (lo <= v <= hi): raise ValueError(f'{key} 값은 {lo}~{hi} 범위여야 합니다.')
        out[key] = v
    num('budget', 0, 10_000_000_000); num('trade_budget', 0, 10_000_000_000)
    num('long_budget', 0, 10_000_000_000); num('saving_goal_krw', 100_000, 10_000_000_000); num('monthly_saving_krw', 10_000, 1_000_000_000); num('risk_pct', 0, 10)
    num('stop_pct', 0.2, 30); num('min_rrr', 0.5, 10)
    num('search_avg_price', 0, 10_000_000_000); num('search_held_qty', 0, 10_000_000)
    try: out['top_n'] = max(10, min(int(float(out.get('top_n', 60))), 150))
    except (TypeError, ValueError): raise ValueError('top_n 값이 올바르지 않습니다.')
    if out.get('trust_mode') not in {'conservative','balanced','aggressive'}: out['trust_mode']='balanced'
    if out.get('mode') not in {'curated','popular','top','smart','mixed'}: out['mode']='smart'
    out['held'] = str(out.get('held',''))[:2000]
    return out


_inflight = {}


def _cached_analysis(key):
    if not CACHE_TTL:
        return None
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.time() - cached['ts'] <= CACHE_TTL:
            return cached
    return None


@app.post('/api/analyze')
def api_analyze():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401

    payload = request.get_json(silent=True) or {}
    try:
        settings = _validated_settings(payload)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e), 'code': 'INVALID_SETTINGS'}), 400
    key = _payload_hash(settings)

    def cached_response(cached):
        return jsonify({'ok': True, **cached['data'], 'cache_hit': True, 'generated_at': cached['generated_at'],
                        'version': APP_VERSION, 'sessions': _market_sessions()})

    # V78.12.0: serve cache before rate limiting (cheap), and coalesce identical in-flight
    # requests (startup analysis + auto report) into one computation.
    cached = _cached_analysis(key)
    if cached:
        return cached_response(cached)
    with _cache_lock:
        event = _inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _inflight[key] = event
    if not owner:
        event.wait(timeout=170)
        cached = _cached_analysis(key)
        if cached:
            return cached_response(cached)
        return jsonify({'ok': False, 'error': '동일 분석이 진행 중입니다. 잠시 후 다시 시도하세요.', 'version': APP_VERSION}), 503

    try:
        ip = _client_ip()
        now = time.time()
        previous = _last_request_by_ip.get(ip, 0.0)
        if MIN_REQUEST_GAP and now - previous < MIN_REQUEST_GAP:
            return jsonify({'ok': False, 'error': '분석 버튼을 너무 빠르게 연속 실행했습니다. 잠시 후 다시 눌러주세요.'}), 429
        _last_request_by_ip[ip] = now
        if len(_last_request_by_ip) > 500:
            for k in [k for k, v in _last_request_by_ip.items() if now - v > 600]:
                _last_request_by_ip.pop(k, None)
        data = analyze(settings)
        generated_at = _now_iso()
        if CACHE_TTL:
            with _cache_lock:
                _cache[key] = {'ts': time.time(), 'data': data, 'generated_at': generated_at}
                if len(_cache) > 20:
                    for old_key, _ in sorted(_cache.items(), key=lambda kv: kv[1]['ts'])[:5]:
                        _cache.pop(old_key, None)
        return jsonify({'ok': True, **data, 'cache_hit': False, 'generated_at': generated_at,
                        'version': APP_VERSION, 'sessions': _market_sessions()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500
    finally:
        with _cache_lock:
            _inflight.pop(key, None)
        event.set()


@app.post('/api/search')
def api_search():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401

    payload = request.get_json(silent=True) or {}
    query = str(payload.pop('query', '')).strip()
    market_hint = str(payload.pop('market_hint', 'AUTO')).strip().upper()
    if not query:
        return jsonify({'ok': False, 'error': '종목명 또는 종목코드를 입력하세요.'}), 400

    try:
        settings = _validated_settings(payload)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e), 'code': 'INVALID_SETTINGS'}), 400
    cache_key = hashlib.sha256((query.upper() + '|' + market_hint + '|' + _payload_hash(settings)).encode('utf-8')).hexdigest()
    now = time.time()
    if SEARCH_CACHE_TTL:
        with _cache_lock:
            cached = _search_cache.get(cache_key)
            if cached and now - cached['ts'] <= SEARCH_CACHE_TTL:
                return jsonify({'ok': True, **cached['data'], 'cache_hit': True,
                                'generated_at': cached['generated_at'], 'version': APP_VERSION,
                                'sessions': _market_sessions()})
    try:
        data = analyze_search(query, settings, market_hint=market_hint)
        generated_at = _now_iso()
        if SEARCH_CACHE_TTL:
            with _cache_lock:
                _search_cache[cache_key] = {'ts': now, 'data': data, 'generated_at': generated_at}
                if len(_search_cache) > 180:
                    for k, _ in sorted(_search_cache.items(), key=lambda kv: kv[1]['ts'])[:40]:
                        _search_cache.pop(k, None)
        return jsonify({'ok': True, **data, 'cache_hit': False, 'generated_at': generated_at,
                        'version': APP_VERSION, 'sessions': _market_sessions()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500



def _to_number(text, default=0.0):
    raw = str(text or '').replace(',', '').replace('%', '').replace('+', '').replace('−', '-').replace('▲','').replace('▼','').strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _fetch_naver_risers(sosok: int, limit: int = 40):
    """Fetch KOSPI/KOSDAQ top risers from Naver Finance.

    This is a display/discovery source only. The app still re-runs its own per-symbol
    analysis before showing any trading signal. Naver quotes can be delayed.
    """
    url = f'https://finance.naver.com/sise/sise_rise.naver?sosok={int(sosok)}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36',
        'Referer': 'https://finance.naver.com/'
    }
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    # Naver Finance legacy pages are commonly EUC-KR encoded.
    if not resp.encoding or str(resp.encoding).lower() in {'iso-8859-1', 'ascii'}:
        resp.encoding = resp.apparent_encoding or 'euc-kr'
    tree = lxml_html.fromstring(resp.text)
    market_name = 'KOSPI' if int(sosok) == 0 else 'KOSDAQ'
    out = []
    for tr in tree.xpath('//table[contains(@class,"type_2")]//tr'):
        anchors = tr.xpath('.//a[contains(@href,"code=")]')
        if not anchors:
            continue
        a = anchors[0]
        href = a.get('href') or ''
        m = re.search(r'code=(\d{6})', href)
        if not m:
            continue
        code = m.group(1)
        name = ' '.join(''.join(a.itertext()).split())
        cells = [' '.join(''.join(td.itertext()).split()) for td in tr.xpath('./td')]
        if len(cells) < 6:
            continue
        price = int(_to_number(cells[2], 0)) if len(cells) > 2 else 0
        change_pct = _to_number(cells[4], 0) if len(cells) > 4 else 0.0
        volume = int(_to_number(cells[5], 0)) if len(cells) > 5 else 0
        # Naver's 거래대금 column is normally displayed in 백만원. If layout changes,
        # fall back to current price × volume as an approximate turnover.
        turnover = 0
        if len(cells) > 8:
            turnover = int(_to_number(cells[8], 0) * 1_000_000)
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = int(price * volume)
        out.append({
            'code': code,
            'name': name or code,
            'market': 'KR',
            'exchange': market_name,
            'price': price,
            'change_pct': round(change_pct, 2),
            'volume': volume,
            'turnover_krw': turnover,
            'source': 'Naver Finance',
        })
        if len(out) >= max(10, min(int(limit), 80)):
            break
    return out


def _is_common_surge_candidate(name):
    """Remove products/special-purpose names from the default surge discovery lane.

    This is only a discovery filter, not a statement about investment quality. Users can turn it
    off from the UI when they deliberately want preferred shares, SPACs, ETFs or ETNs included.
    """
    n = re.sub(r'\s+', '', str(name or '')).upper()
    if not n:
        return False
    if any(tok in n for tok in ('ETF', 'ETN', '스팩', 'SPAC', '인버스', '레버리지')):
        return False
    if re.search(r'(?:\d?우(?:B|C)?|우)$', n):
        return False
    return True


@app.get('/api/surge-stocks')
def api_surge_stocks():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    market = str(request.args.get('market', 'ALL')).upper()
    if market not in {'ALL','KOSPI','KOSDAQ'}:
        market = 'ALL'
    common_only = str(request.args.get('common_only', '1')).lower() not in {'0','false','no'}
    try:
        min_change = max(0.0, min(float(request.args.get('min_change', 5)), 29.99))
    except (TypeError, ValueError):
        min_change = 5.0
    try:
        min_turnover_eok = max(0.0, min(float(request.args.get('min_turnover_eok', 10)), 100000))
    except (TypeError, ValueError):
        min_turnover_eok = 10.0
    try:
        limit = max(5, min(int(request.args.get('limit', 30)), 60))
    except (TypeError, ValueError):
        limit = 30

    key = f'{market}:{min_change:.2f}:{min_turnover_eok:.2f}:{limit}:{int(common_only)}'
    now = time.time()
    cached = _surge_cache.get(key)
    if cached and now - cached['ts'] < 45:
        return jsonify({'ok': True, **cached['data'], 'cache_hit': True, 'version': APP_VERSION})

    try:
        raw = []
        if market in {'ALL', 'KOSPI'}:
            raw.extend(_fetch_naver_risers(0, max(limit, 30)))
        if market in {'ALL', 'KOSDAQ'}:
            raw.extend(_fetch_naver_risers(1, max(limit, 30)))
        min_turnover = min_turnover_eok * 100_000_000
        seen = set(); items = []
        for x in raw:
            code = str(x.get('code') or '')
            if not code or code in seen:
                continue
            seen.add(code)
            if float(x.get('change_pct') or 0) < min_change or int(x.get('turnover_krw') or 0) < min_turnover:
                continue
            if common_only and not _is_common_surge_candidate(x.get('name')):
                continue
            row = dict(x)
            row['discovery_only'] = True
            items.append(row)
        items.sort(key=lambda x: (float(x.get('change_pct') or 0), int(x.get('turnover_krw') or 0)), reverse=True)
        items = items[:limit]
        payload = {
            'items': items,
            'generated_at': _now_iso(),
            'market_session': _market_session('KR'),
            'filters': {'market': market, 'min_change_pct': min_change, 'min_turnover_eok': min_turnover_eok, 'limit': limit,
                        'common_only': common_only},
            'source_note': '네이버 금융 등락률 페이지 기반 탐색값입니다. 장중 시세는 지연될 수 있고, 장 마감·휴장 시 최근 거래일 값일 수 있으며 기술분석 가격과 시점이 다를 수 있습니다. 급등 자체는 자동매수 신호가 아닙니다.'
        }
        _surge_cache[key] = {'ts': now, 'data': payload}
        if len(_surge_cache) > 30:
            for k, _ in sorted(_surge_cache.items(), key=lambda kv: kv[1]['ts'])[:10]:
                _surge_cache.pop(k, None)
        return jsonify({'ok': True, **payload, 'cache_hit': False, 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'급등주 원본 조회 실패: {str(e)[:220]}', 'items': [], 'version': APP_VERSION}), 502


@app.get('/api/symbols')
def api_symbols():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    q=str(request.args.get('q','')).strip(); market=str(request.args.get('market','AUTO')).upper()
    if not q: return jsonify({'ok':True,'items':[],'version':APP_VERSION})
    try:
        return jsonify({'ok':True,'items':search_instruments(q,market,12),'version':APP_VERSION})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)[:200],'items':[],'version':APP_VERSION}),500

@app.post('/api/account/register')
def api_account_register():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    payload = request.get_json(silent=True) or {}
    account_id = _normalize_account_id(payload.get('account_id'))
    password = str(payload.get('password') or '')
    if len(account_id) < 6:
        return jsonify({'ok': False, 'error': '개인 계정 ID는 영문/숫자 기준 6자 이상으로 만들어주세요.'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': '개인 계정 비밀번호는 8자 이상으로 만들어주세요.'}), 400
    with _account_lock:
        accounts = _read_accounts()
        if account_id in accounts:
            return jsonify({'ok': False, 'error': '이미 사용 중인 개인 계정 ID입니다.'}), 409
        salt = os.urandom(16).hex(); iterations = PASSWORD_ITERATIONS
        accounts[account_id] = {
            'salt': salt, 'iterations': iterations,
            'password_hash': _password_hash(password, salt, iterations),
            'created_at': _now_iso()
        }
        _write_accounts(accounts)
    cookie_value, expires_at = _issue_account_session(account_id)
    resp = jsonify({'ok': True, 'account_id': account_id, 'version': APP_VERSION, 'session_persistent': bool(cookie_value)})
    return _set_account_cookie(resp, cookie_value, expires_at)

@app.get('/api/account/status')
def api_account_status():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    owner, error = _account_auth()
    if error:
        code = ('ACCOUNT_NOT_FOUND' if '등록되지 않은' in error else
                'ACCOUNT_THROTTLED' if '제한' in error else
                'ACCOUNT_BAD_PASSWORD' if '비밀번호' in error else 'ACCOUNT_REQUIRED')
        orphaned_rows = 0
        if code == 'ACCOUNT_NOT_FOUND':
            aid = _normalize_account_id(request.headers.get('X-Account-ID'))
            if aid:
                orphan_owner = _owner_for(aid)
                with _portfolio_lock:
                    orphan_rec = _read_portfolio_store().get(orphan_owner, {})
                orphan_rows = orphan_rec.get('rows', []) if isinstance(orphan_rec, dict) else []
                orphaned_rows = len(orphan_rows) if isinstance(orphan_rows, list) else 0
        return jsonify({'ok': False, 'error': error, 'code': code, 'orphaned_server_rows': orphaned_rows, 'storage': _storage_status()}), 401
    account_id = getattr(g, 'account_id', '')
    auth_via = getattr(g, 'account_auth_via', '')
    cookie_value = None
    if auth_via == 'password':
        cookie_value, expires_at = _issue_account_session(account_id)
    resp = jsonify({'ok': True, 'account_id': account_id, 'version': APP_VERSION,
                    'auth_via': auth_via, 'session_persistent': True, 'storage': _storage_status()})
    return _set_account_cookie(resp, cookie_value) if cookie_value else resp

@app.post('/api/account/recover-local')
def api_account_recover_local():
    """Re-create a missing server account only when its server portfolio is also empty.

    This is intentionally conservative: if server-side rows still exist for the ID, recovery is
    refused because the server no longer has a password verifier to prove ownership. The user
    should restore the account database/backup instead. If both registry and portfolio were lost
    by an ephemeral deploy, a browser holding a local portfolio can safely create a fresh empty
    server namespace and upload its local copy immediately afterwards.
    """
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    payload = request.get_json(silent=True) or {}
    account_id = _normalize_account_id(payload.get('account_id'))
    password = str(payload.get('password') or '')
    local_count = max(0, min(100, int(payload.get('local_count') or 0)))
    if len(account_id) < 6 or len(password) < 8:
        return jsonify({'ok': False, 'error': 'ID 6자 이상, 비밀번호 8자 이상이 필요합니다.'}), 400
    if local_count < 1:
        return jsonify({'ok': False, 'error': '이 기기에 복구할 보유/관심종목 자료가 없습니다.', 'code': 'NO_LOCAL_RECOVERY_DATA'}), 400
    owner = _owner_for(account_id)
    with _account_lock:
        accounts = _read_accounts()
        if account_id in accounts:
            return jsonify({'ok': False, 'error': '서버에 계정이 존재합니다. 일반 계정 연결을 사용하세요.', 'code': 'ACCOUNT_EXISTS'}), 409
        with _portfolio_lock:
            store = _read_portfolio_store()
            existing = store.get(owner, {})
            existing_rows = existing.get('rows', []) if isinstance(existing, dict) else []
            if isinstance(existing_rows, list) and existing_rows:
                return jsonify({'ok': False, 'error': '서버에는 기존 자산이 남아 있지만 계정 인증기록이 없습니다. 보안을 위해 자동 재등록하지 않습니다.', 'code': 'SERVER_DATA_ORPHANED'}), 409
        salt = os.urandom(16).hex(); iterations = PASSWORD_ITERATIONS
        accounts[account_id] = {
            'salt': salt, 'iterations': iterations,
            'password_hash': _password_hash(password, salt, iterations),
            'created_at': _now_iso(), 'recovered_from_local_at': _now_iso()
        }
        _write_accounts(accounts)
    cookie_value, expires_at = _issue_account_session(account_id)
    resp = jsonify({'ok': True, 'account_id': account_id, 'version': APP_VERSION,
                    'recovered': True, 'storage': _storage_status()})
    return _set_account_cookie(resp, cookie_value, expires_at)


@app.get('/api/storage/status')
def api_storage_status():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    return jsonify({'ok': True, 'version': APP_VERSION, 'storage': _storage_status()})


@app.post('/api/storage/verify')
def api_storage_verify():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    probe = _state_store.probe_database()
    storage = _storage_status()
    return jsonify({'ok': bool(probe.get('ok')), 'version': APP_VERSION, 'probe': probe, 'storage': storage}), (200 if probe.get('ok') else 503)


@app.post('/api/account/change-id')
def api_account_change_id():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    owner, error = _account_auth()
    old_id = getattr(g, 'account_id', _normalize_account_id(request.headers.get('X-Account-ID')))
    if error:
        return jsonify({'ok': False, 'error': error}), 401
    payload = request.get_json(silent=True) or {}
    new_id = _normalize_account_id(payload.get('new_account_id'))
    if len(new_id) < 6:
        return jsonify({'ok': False, 'error': '새 개인 계정 ID는 6자 이상이어야 합니다.'}), 400
    if new_id == old_id:
        return jsonify({'ok': True, 'account_id': old_id, 'version': APP_VERSION})
    new_owner = _owner_for(new_id)
    # Account record and portfolio ownership are moved together. If the portfolio write fails, roll back the account file.
    with _account_lock:
        accounts = _read_accounts()
        rec = accounts.get(old_id)
        if not isinstance(rec, dict):
            return jsonify({'ok': False, 'error': '현재 개인 계정을 찾을 수 없습니다.'}), 404
        if new_id in accounts:
            return jsonify({'ok': False, 'error': '이미 사용 중인 개인 계정 ID입니다.'}), 409
        original_accounts = dict(accounts)
        moved = dict(rec); moved['updated_at'] = _now_iso(); moved['renamed_from'] = old_id
        accounts[new_id] = moved
        del accounts[old_id]
        try:
            _write_accounts(accounts)
            with _portfolio_lock:
                store = _read_portfolio_store()
                if new_owner in store and new_owner != owner:
                    raise ValueError('새 ID 저장공간이 이미 사용 중입니다.')
                if owner in store:
                    store[new_owner] = store.pop(owner)
                    _write_portfolio_store(store)
        except Exception as exc:
            try:
                _write_accounts(original_accounts)
            except Exception:
                pass
            return jsonify({'ok': False, 'error': f'ID 변경 중 저장 오류가 발생했습니다: {str(exc)[:120]}'}), 500
    cookie_value, expires_at = _issue_account_session(new_id)
    resp = jsonify({'ok': True, 'account_id': new_id, 'version': APP_VERSION})
    return _set_account_cookie(resp, cookie_value, expires_at)

@app.post('/api/account/change-password')
def api_account_change_password():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    _owner, error = _account_auth()
    if error:
        return jsonify({'ok': False, 'error': error}), 401
    account_id = getattr(g, 'account_id', _normalize_account_id(request.headers.get('X-Account-ID')))
    payload = request.get_json(silent=True) or {}
    new_password = str(payload.get('new_password') or '')
    if len(new_password) < 8:
        return jsonify({'ok': False, 'error': '새 비밀번호는 8자 이상이어야 합니다.'}), 400
    with _account_lock:
        accounts = _read_accounts()
        rec = accounts.get(account_id)
        if not isinstance(rec, dict):
            return jsonify({'ok': False, 'error': '현재 개인 계정을 찾을 수 없습니다.'}), 404
        salt = os.urandom(16).hex(); iterations = PASSWORD_ITERATIONS
        rec = dict(rec)
        rec.update({'salt': salt, 'iterations': iterations, 'password_hash': _password_hash(new_password, salt, iterations), 'updated_at': _now_iso(), 'sessions': []})
        accounts[account_id] = rec
        _write_accounts(accounts)
    cookie_value, expires_at = _issue_account_session(account_id)
    resp = jsonify({'ok': True, 'account_id': account_id, 'version': APP_VERSION})
    return _set_account_cookie(resp, cookie_value, expires_at)

@app.post('/api/account/logout')
def api_account_logout():
    # Revoke only the current browser session when possible, then clear its cookie.
    cookie = str(request.cookies.get(ACCOUNT_COOKIE) or '')
    if '.' in cookie:
        account_id, token = cookie.split('.', 1)
        account_id = _normalize_account_id(account_id)
        token_hash = _session_token_hash(token)
        with _account_lock:
            accounts = _read_accounts()
            rec = accounts.get(account_id)
            if isinstance(rec, dict):
                rec = _prune_sessions(dict(rec))
                rec['sessions'] = [x for x in rec.get('sessions', []) if str(x.get('token_hash')) != token_hash]
                accounts[account_id] = rec
                _write_accounts(accounts)
    resp = jsonify({'ok': True, 'version': APP_VERSION})
    return _clear_account_cookie(resp)

@app.get('/api/portfolio')
def api_portfolio_get():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    owner, account_error = _account_auth()
    if account_error:
        return jsonify({'ok': False, 'error': account_error, 'code': 'ACCOUNT_REQUIRED'}), 401
    with _portfolio_lock:
        store = _read_portfolio_store()
        record = store.get(owner, {})
    rows = record.get('rows', []) if isinstance(record, dict) else []
    rows = rows if isinstance(rows, list) else []
    storage = _storage_status()
    return jsonify({'ok': True, 'rows': rows,
                    'row_count': len(rows), 'checksum': _portfolio_digest(rows),
                    'updated_at': record.get('updated_at') if isinstance(record, dict) else None,
                    'persistent': storage.get('durability') in {'database','persistent-disk'},
                    'storage': storage,
                    'account_id': getattr(g, 'account_id', ''), 'auth_via': getattr(g, 'account_auth_via', ''),
                    'version': APP_VERSION})

def _num(value, default=0.0):
    try:
        v = float(value if value not in (None, '') else default)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, v) if v == v and v not in (float('inf'), float('-inf')) else float(default)


# Only the fields the portfolio/verdict UI reads are persisted.  The previous build stored the
# full analysis object (backtest, split plan, notes ...) for every row inside one shared JSON
# blob, so every save rewrote megabytes for all users.
_LAST_ITEM_KEYS = {
    'ok', 'code', 'name', 'market', 'category', 'sector', 'sector_major', 'source_sector', 'theme_tags',
    'grade', 'decision', 'price_krw', 'price_native', 'currency', 'entry_krw', 'target1_krw', 'stop1_krw',
    'qty', 'qty_capacity', 'rrr', 'momentum', 'rsi', 'opportunity_score', 'opportunity_eligible',
    'adaptive_candidate', 'near_buy_candidate', 'allocation_wait', 'candidate_reason', 'buyable_rank',
    'market_breadth_score', 'market_breadth_label', 'hard_block', 'risk_flags', 'held_action',
    'held_price_krw', 'held_qty', 'held_pnl_pct', 'data_quality_score', 'buy_time', 'tech',
    'holding_input_currency', 'holding_input_price_native',
}


def _slim_last(last):
    if not isinstance(last, dict):
        return None
    item = last.get('item') if isinstance(last.get('item'), dict) else {}
    slim = {k: item.get(k) for k in _LAST_ITEM_KEYS if k in item}
    dq = item.get('data_quality')
    if isinstance(dq, dict):
        slim['data_quality'] = {'score': dq.get('score')}
    bt = item.get('backtest')
    if isinstance(bt, dict):
        slim['backtest'] = {'validation_score': bt.get('validation_score'), 'max_drawdown_pct': bt.get('max_drawdown_pct')}
    return {'item': slim, 'at': str(last.get('at') or '')[:40]}


@app.put('/api/portfolio')
def api_portfolio_put():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    owner, account_error = _account_auth()
    if account_error:
        return jsonify({'ok': False, 'error': account_error, 'code': 'ACCOUNT_REQUIRED'}), 401
    payload = request.get_json(silent=True) or {}
    rows = payload.get('rows', [])
    if not isinstance(rows, list):
        return jsonify({'ok': False, 'error': '보유/관심종목 데이터 형식이 올바르지 않습니다.'}), 400
    clean = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        typ = row.get('type') if row.get('type') in {'held','watch'} else 'watch'
        query = str(row.get('query','')).strip()[:80]
        if not query:
            continue
        market = str(row.get('market','AUTO')).upper()
        if market not in {'AUTO','KR','ETF','US'}: market='AUTO'
        avg = _num(row.get('avg')); qty = _num(row.get('qty')); daily = _num(row.get('daily_amount'))
        horizon = max(1, min(120, int(_num(row.get('horizon_months'), 24) or 24)))
        target_amount = _num(row.get('target_amount'))
        clean.append({'id': str(row.get('id',''))[:180] or f'{typ}|{market}|{query.upper()}',
                      'type':typ,'query':query,'market':market,
                      'avg':avg if typ=='held' else 0,'qty':qty if typ=='held' else 0,
                      'name':str(row.get('name','')).strip()[:120], 'code':str(row.get('code','')).strip()[:24],
                      'instrument_type':str(row.get('instrument_type','')).strip()[:20], 'exchange':str(row.get('exchange','')).strip()[:30],
                      'sector':str(row.get('sector','')).strip()[:80], 'sector_major':str(row.get('sector_major','')).strip()[:80],
                      'source_sector':str(row.get('source_sector','')).strip()[:120],
                      'theme_tags':[str(x).strip()[:40] for x in (row.get('theme_tags') or [])[:6] if str(x).strip()],
                      'sector_updated_at':str(row.get('sector_updated_at','')).strip()[:40],
                      'accumulate':bool(row.get('accumulate',False)) if typ=='held' else False,
                      'daily_amount':daily if typ=='held' else 0,
                      'accum_start':str(row.get('accum_start','')).strip()[:10] if typ=='held' else '',
                      'horizon_months': horizon if typ=='held' else 0,
                      'target_amount': target_amount if typ=='held' else 0,
                      'added_at':str(row.get('added_at') or _now_iso())[:40],
                      'last':_slim_last(row.get('last'))})
    updated=_now_iso(); checksum=_portfolio_digest(clean)
    try:
        with _portfolio_lock:
            store=_read_portfolio_store()
            store[owner]={'rows':clean,'updated_at':updated,'checksum':checksum}
            _write_portfolio_store(store)
            # Immediate read-back verification catches disk/write problems before the UI reports success.
            verify_store=_read_portfolio_store(); verify_record=verify_store.get(owner,{})
            verify_rows=verify_record.get('rows',[]) if isinstance(verify_record,dict) else []
            verified=isinstance(verify_rows,list) and len(verify_rows)==len(clean) and _portfolio_digest(verify_rows)==checksum
        if not verified:
            return jsonify({'ok':False,'error':'서버 저장 후 재검증에 실패했습니다.','code':'SAVE_VERIFY_FAILED','version':APP_VERSION}),500
    except OSError as exc:
        return jsonify({'ok':False,'error':f'서버 저장소 쓰기 오류: {str(exc)[:120]}','code':'STORAGE_WRITE_FAILED','version':APP_VERSION}),500
    return jsonify({'ok':True,'rows':clean,'row_count':len(clean),'checksum':checksum,'verified':True,'updated_at':updated,'storage':_storage_status(),'version':APP_VERSION})

@app.delete('/api/portfolio')
def api_portfolio_delete():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    owner, account_error = _account_auth()
    if account_error:
        return jsonify({'ok': False, 'error': account_error, 'code': 'ACCOUNT_REQUIRED'}), 401
    with _portfolio_lock:
        store = _read_portfolio_store()
        store.pop(owner, None)
        _write_portfolio_store(store)
    return jsonify({'ok': True, 'version': APP_VERSION})


@app.get('/api/portfolio/legacy')
def api_portfolio_legacy_get():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.'}), 401
    with _portfolio_lock:
        store = _read_portfolio_store(); record = store.get(_portfolio_owner_legacy(), {})
    rows = record.get('rows', []) if isinstance(record, dict) else []
    return jsonify({'ok': True, 'rows': rows if isinstance(rows, list) else [], 'version': APP_VERSION})


def _cached_extra(cache, key, ttl, loader, max_items):
    """Small provider cache. Provider failures are never kept for the full success TTL."""
    now = time.time()
    with _extra_cache_lock:
        cached = cache.get(key)
        if cached and now - cached['ts'] < cached.get('ttl', ttl):
            return cached['data'], True
    data = loader()
    # Best-effort provider helpers may return an error field instead of raising. A transient
    # outage should therefore expire quickly rather than poisoning a report for 15-30 minutes.
    effective_ttl = min(ttl, 60) if isinstance(data, dict) and data.get('error') else ttl
    with _extra_cache_lock:
        cache[key] = {'ts': time.time(), 'ttl': effective_ttl, 'data': data}
        if len(cache) > max_items:
            for k, _ in sorted(cache.items(), key=lambda kv: kv[1]['ts'])[:max(10, max_items // 4)]:
                cache.pop(k, None)
    return data, False


def _fundamental_data(code, market):
    key = f'{market}:{code}'
    return _cached_extra(_fund_cache, key, 1800, lambda: fetch_fundamentals(code, market), 80)


def _issue_data(code, market):
    key = f'{market}:{code}'
    return _cached_extra(_issue_cache, key, 900, lambda: fetch_recent_issues(code, market), 160)

@app.post('/api/fundamentals')
def api_fundamentals():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    payload = request.get_json(silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()
    market = str(payload.get('market', 'KR')).strip().upper()
    if not code:
        return jsonify({'ok': False, 'error': '종목코드가 없습니다.'}), 400
    try:
        data, hit = _fundamental_data(code, market)
        return jsonify({'ok': True, 'data': data, 'cache_hit': hit, 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500


@app.post('/api/issues')
def api_issues():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    payload = request.get_json(silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()
    market = str(payload.get('market', 'KR')).strip().upper()
    if not code:
        return jsonify({'ok': False, 'error': '종목코드가 없습니다.'}), 400
    try:
        data, hit = _issue_data(code, market)
        return jsonify({'ok': True, 'data': data, 'cache_hit': hit, 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500


@app.post('/api/report-data')
def api_report_data():
    """Return fundamentals + news in one browser round trip for the automatic report."""
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    payload = request.get_json(silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()
    market = str(payload.get('market', 'KR')).strip().upper()
    if not code:
        return jsonify({'ok': False, 'error': '종목코드가 없습니다.'}), 400
    result = {'fundamentals': {}, 'issues': {}, 'errors': []}
    hits = {'fundamentals': False, 'issues': False}
    try:
        # Provider calls are independent. Two short-lived threads reduce report latency while the
        # provider cache prevents duplicate outbound calls on repeated report refreshes.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            ff = ex.submit(_fundamental_data, code, market)
            fi = ex.submit(_issue_data, code, market)
            try:
                result['fundamentals'], hits['fundamentals'] = ff.result(timeout=25)
            except Exception as exc:
                result['errors'].append(f'실적: {str(exc)[:120]}')
            try:
                result['issues'], hits['issues'] = fi.result(timeout=25)
            except Exception as exc:
                result['errors'].append(f'뉴스: {str(exc)[:120]}')
        return jsonify({'ok': True, **result, 'cache_hit': hits, 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], **result, 'version': APP_VERSION}), 500


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def open_browser(port):
    webbrowser.open(f'http://127.0.0.1:{port}')


if __name__ == '__main__':
    host = '0.0.0.0'
    if os.environ.get('OPEN_BROWSER') == '1':
        try:
            with open(PID_FILE, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    port = int(os.environ.get('PORT', '8787'))
    if os.environ.get('OPEN_BROWSER') == '1':
        threading.Timer(1.2, lambda: open_browser(port)).start()
    print('\n' + '=' * 66)
    print(f' {APP_VERSION} 모바일/온라인 웹앱')
    print(f' PC 주소 : http://127.0.0.1:{port}')
    print(f' 휴대폰  : http://{local_ip()}:{port}  (로컬 실행 시 같은 Wi-Fi)')
    print(' 온라인 배포 후에는 발급된 https://...onrender.com 주소로 접속')
    print(' 종료: 이 창에서 Ctrl+C')
    print('=' * 66 + '\n')
    app.run(host=host, port=port, debug=False, threaded=True)
