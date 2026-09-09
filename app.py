import hashlib
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
from engine import analyze, analyze_search, search_instruments, fetch_fundamentals, fetch_recent_issues

APP_VERSION = 'V78.7.2'
app = Flask(__name__)

_cache_lock = threading.Lock()
_cache = {}
_last_request_by_ip = {}
_fund_cache = {}
_issue_cache = {}
CACHE_TTL = max(0, int(os.environ.get('CACHE_TTL_SECONDS', '120')))
MIN_REQUEST_GAP = max(0.0, float(os.environ.get('MIN_REQUEST_GAP_SECONDS', '2')))
APP_PIN = os.environ.get('APP_PIN', '').strip()

# Cross-device portfolio/watchlist sync. Point DATA_DIR at a persistent disk in production.
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
PORTFOLIO_BACKUP_FILE = os.path.join(DATA_DIR, 'portfolio.backup.json')
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')
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

def _set_account_cookie(response, cookie_value, expires_at=None):
    if not cookie_value:
        return response
    response.set_cookie(ACCOUNT_COOKIE, cookie_value, max_age=ACCOUNT_SESSION_DAYS*86400,
                        httponly=True, samesite='Lax', secure=bool(request.is_secure), path='/')
    return response

def _clear_account_cookie(response):
    response.delete_cookie(ACCOUNT_COOKIE, path='/', samesite='Lax', secure=bool(request.is_secure))
    return response

def _normalize_account_id(value):
    raw = str(value or '').strip().lower()
    return ''.join(ch for ch in raw if ch.isalnum() or ch in {'-','_','.'})[:40]

def _read_accounts():
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        try:
            with open(PORTFOLIO_BACKUP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

def _write_accounts(data):
    tmp = ACCOUNTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, ACCOUNTS_FILE)

def _password_hash(password, salt_hex, iterations=180000):
    return hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), bytes.fromhex(salt_hex), iterations).hex()

def _account_auth():
    # 1) Prefer a persistent HttpOnly cookie session. This keeps mobile/PWA login alive
    # without storing the password in localStorage/sessionStorage.
    header_account_id = _normalize_account_id(request.headers.get('X-Account-ID'))
    header_password = str(request.headers.get('X-Account-Password') or '')
    cookie = str(request.cookies.get(ACCOUNT_COOKIE) or '')
    # Explicit credentials mean the user is deliberately connecting/switching accounts.
    if not (header_account_id and header_password) and '.' in cookie:
        cookie_id, token = cookie.split('.', 1)
        account_id = _normalize_account_id(cookie_id)
        token_hash = _session_token_hash(token)
        with _account_lock:
            accounts = _read_accounts()
            rec = accounts.get(account_id)
            if isinstance(rec, dict):
                rec = _prune_sessions(dict(rec))
                valid = any(str(x.get('token_hash')) == token_hash for x in rec.get('sessions', []))
                accounts[account_id] = rec
                _write_accounts(accounts)
            else:
                valid = False
        if valid:
            g.account_id = account_id
            g.account_auth_via = 'cookie'
            owner = hashlib.sha256(('account:' + account_id).encode('utf-8')).hexdigest()[:32]
            return owner, None

    # 2) Fall back to explicit ID/password for first login or another device.
    account_id = header_account_id
    password = header_password
    if not account_id or not password:
        return None, '개인 계정 연결이 필요합니다.'
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
    import hmac
    if not hmac.compare_digest(actual, expected):
        return None, '개인 계정 비밀번호가 올바르지 않습니다.'
    g.account_id = account_id
    g.account_auth_via = 'password'
    owner = hashlib.sha256(('account:' + account_id).encode('utf-8')).hexdigest()[:32]
    return owner, None

def _portfolio_owner_legacy():
    # V78.5 이하의 기존 동기화 공간. 신규 개인계정으로 이관할 때만 사용합니다.
    sync_key = (request.headers.get('X-Sync-Key') or '').strip()
    seed = ('sync:' + sync_key) if sync_key else ('legacy:' + (APP_PIN or 'local-default'))
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]

def _read_portfolio_store():
    try:
        with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _write_portfolio_store(data):
    # Keep the last valid full store before replacement so an app upgrade cannot silently destroy portfolios.
    try:
        if os.path.exists(PORTFOLIO_FILE) and os.path.getsize(PORTFOLIO_FILE) > 2:
            with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as src:
                previous = json.load(src)
            if isinstance(previous, dict):
                btmp = PORTFOLIO_BACKUP_FILE + '.tmp'
                with open(btmp, 'w', encoding='utf-8') as bf:
                    json.dump(previous, bf, ensure_ascii=False, separators=(',', ':')); bf.flush(); os.fsync(bf.fileno())
                os.replace(btmp, PORTFOLIO_BACKUP_FILE)
    except Exception:
        pass
    tmp = PORTFOLIO_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':')); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, PORTFOLIO_FILE)

def _portfolio_digest(rows):
    payload = json.dumps(rows if isinstance(rows, list) else [], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


# Market clocks are calculated on the server with explicit time zones.
# This avoids Render's UTC clock (or a browser/PWA clock quirk) being mistaken for KRX time.
KR_HOLIDAYS_2026 = {
    '2026-01-01','2026-02-16','2026-02-17','2026-02-18','2026-03-02',
    '2026-05-05','2026-05-25','2026-06-03','2026-08-17',
    '2026-09-24','2026-09-25','2026-10-05','2026-10-09','2026-12-25'
}
US_HOLIDAYS_2026 = {
    '2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25',
    '2026-06-19','2026-07-03','2026-09-07','2026-11-26','2026-12-25'
}

def _market_session(market):
    """Return market phase with regular-session truth kept separate from extended-hours availability.

    `open` intentionally means *regular session open* so recommendation logic never upgrades an
    extended-hours observation into an immediate-buy signal.
    """
    market = 'US' if str(market).upper() == 'US' else 'KR'
    tz = ZoneInfo('America/New_York') if market == 'US' else ZoneInfo('Asia/Seoul')
    now = datetime.now(tz)
    date_key = now.strftime('%Y-%m-%d')
    weekend = now.weekday() >= 5
    holiday = date_key in (US_HOLIDAYS_2026 if market == 'US' else KR_HOLIDAYS_2026)
    mins = now.hour * 60 + now.minute
    state = 'closed'; label = '장 마감'; regular_open = False; extended_open = False

    if weekend or holiday:
        state = 'closed'; label = '주말 휴장' if weekend else '공휴일 휴장'
    elif market == 'KR':
        # KRX: opening-auction order receipt 08:30~09:00, regular 09:00~15:30,
        # post-market sessions begin again at 15:40 and continue to 18:00.
        if mins < 8 * 60 + 30:
            state = 'pre_wait'; label = '개장 전'
        elif mins < 9 * 60:
            state = 'preopen'; label = '시가 동시호가'
        elif mins < 15 * 60 + 30:
            state = 'regular'; label = '정규장 거래중'; regular_open = True
        elif mins < 15 * 60 + 40:
            state = 'post_wait'; label = '정규장 종료·시간외 대기'
        elif mins < 18 * 60:
            state = 'post'; label = '시간외 거래'; extended_open = True
        else:
            state = 'after'; label = '장 마감'
    else:
        # Nasdaq: pre-market 04:00~09:30 ET, regular 09:30~16:00 ET, after-hours 16:00~20:00 ET.
        if mins < 4 * 60:
            state = 'pre_wait'; label = '프리마켓 대기'
        elif mins < 9 * 60 + 30:
            state = 'premarket'; label = '프리마켓'; extended_open = True
        elif mins < 16 * 60:
            state = 'regular'; label = '정규장 거래중'; regular_open = True
        elif mins < 20 * 60:
            state = 'afterhours'; label = '애프터마켓'; extended_open = True
        else:
            state = 'after'; label = '장 마감'

    return {
        'market': market, 'open': regular_open, 'regular_open': regular_open,
        'extended_open': extended_open, 'tradable': regular_open or extended_open,
        'state': state, 'label': label, 'date': date_key,
        'local_time': now.strftime('%H:%M'),
        'timezone': 'America/New_York' if market == 'US' else 'Asia/Seoul',
        'policy': 'regular_session_only_for_immediate_signal'
    }

def _market_sessions():
    return {'KR': _market_session('KR'), 'US': _market_session('US')}

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
    return supplied == APP_PIN


@app.get('/')
def home():
    return render_template('index.html', version=APP_VERSION, pin_required=bool(APP_PIN))


@app.get('/health')
def health():
    return jsonify({
        'ok': True,
        'version': APP_VERSION,
        'time': _now_iso(),
        'pin_required': bool(APP_PIN),
        'cache_ttl_seconds': CACHE_TTL,
        'sessions': _market_sessions(),
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


@app.post('/api/analyze')
def api_analyze():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401

    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    now = time.time()
    previous = _last_request_by_ip.get(ip, 0.0)
    if MIN_REQUEST_GAP and now - previous < MIN_REQUEST_GAP:
        return jsonify({'ok': False, 'error': '분석 버튼을 너무 빠르게 연속 실행했습니다. 잠시 후 다시 눌러주세요.'}), 429
    _last_request_by_ip[ip] = now

    payload = request.get_json(silent=True) or {}
    try:
        settings = _validated_settings(payload)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e), 'code': 'INVALID_SETTINGS'}), 400
    key = _payload_hash(settings)

    if CACHE_TTL:
        with _cache_lock:
            cached = _cache.get(key)
            if cached and now - cached['ts'] <= CACHE_TTL:
                return jsonify({
                    'ok': True,
                    **cached['data'],
                    'cache_hit': True,
                    'generated_at': cached['generated_at'],
                    'version': APP_VERSION,
                    'sessions': _market_sessions(),
                })

    try:
        data = analyze(settings)
        generated_at = _now_iso()
        if CACHE_TTL:
            with _cache_lock:
                _cache[key] = {'ts': time.time(), 'data': data, 'generated_at': generated_at}
                # Keep memory bounded on long-running services.
                if len(_cache) > 20:
                    oldest = sorted(_cache.items(), key=lambda kv: kv[1]['ts'])[:5]
                    for old_key, _ in oldest:
                        _cache.pop(old_key, None)
        return jsonify({
            'ok': True,
            **data,
            'cache_hit': False,
            'generated_at': generated_at,
            'version': APP_VERSION,
            'sessions': _market_sessions(),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500


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
    try:
        data = analyze_search(query, settings, market_hint=market_hint)
        return jsonify({'ok': True, **data, 'generated_at': _now_iso(), 'version': APP_VERSION, 'sessions': _market_sessions()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500





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
        salt = os.urandom(16).hex(); iterations = 180000
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
        return jsonify({'ok': False, 'error': error}), 401
    account_id = getattr(g, 'account_id', '')
    auth_via = getattr(g, 'account_auth_via', '')
    cookie_value = None
    if auth_via == 'password':
        cookie_value, expires_at = _issue_account_session(account_id)
    resp = jsonify({'ok': True, 'account_id': account_id, 'version': APP_VERSION,
                    'auth_via': auth_via, 'session_persistent': True})
    return _set_account_cookie(resp, cookie_value) if cookie_value else resp

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
    new_owner = hashlib.sha256(('account:' + new_id).encode('utf-8')).hexdigest()[:32]
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
        salt = os.urandom(16).hex(); iterations = 210000
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
    return jsonify({'ok': True, 'rows': rows,
                    'row_count': len(rows), 'checksum': _portfolio_digest(rows),
                    'updated_at': record.get('updated_at') if isinstance(record, dict) else None,
                    'persistent': os.path.abspath(DATA_DIR).startswith('/var/data'),
                    'account_id': getattr(g, 'account_id', ''), 'auth_via': getattr(g, 'account_auth_via', ''),
                    'version': APP_VERSION})

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
        try:
            avg=max(0.0,float(row.get('avg',0) or 0)); qty=max(0.0,float(row.get('qty',0) or 0)); daily=max(0.0,float(row.get('daily_amount',0) or 0))
        except (TypeError,ValueError):
            avg=qty=daily=0.0
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
                      'horizon_months': max(1,min(120,int(float(row.get('horizon_months',24) or 24)))) if typ=='held' else 0,
                      'target_amount': max(0.0,float(row.get('target_amount',0) or 0)) if typ=='held' else 0,
                      'added_at':str(row.get('added_at') or _now_iso())[:40],
                      'last':row.get('last') if isinstance(row.get('last'),dict) else None})
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
    return jsonify({'ok':True,'rows':clean,'row_count':len(clean),'checksum':checksum,'verified':True,'updated_at':updated,'version':APP_VERSION})

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

@app.post('/api/fundamentals')
def api_fundamentals():
    if not _authorized():
        return jsonify({'ok': False, 'error': '접속 PIN이 올바르지 않습니다.', 'code': 'PIN_REQUIRED'}), 401
    payload = request.get_json(silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()
    market = str(payload.get('market', 'KR')).strip().upper()
    if not code:
        return jsonify({'ok': False, 'error': '종목코드가 없습니다.'}), 400
    key = f'{market}:{code}'
    now = time.time()
    cached = _fund_cache.get(key)
    if cached and now - cached['ts'] < 1800:
        return jsonify({'ok': True, 'data': cached['data'], 'cache_hit': True, 'version': APP_VERSION})
    try:
        data = fetch_fundamentals(code, market)
        _fund_cache[key] = {'ts': now, 'data': data}
        if len(_fund_cache) > 60:
            oldest = sorted(_fund_cache.items(), key=lambda kv: kv[1]['ts'])[:10]
            for k, _ in oldest:
                _fund_cache.pop(k, None)
        return jsonify({'ok': True, 'data': data, 'cache_hit': False, 'version': APP_VERSION})
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
    key = f'{market}:{code}'
    now = time.time()
    cached = _issue_cache.get(key)
    if cached and now - cached['ts'] < 900:
        return jsonify({'ok': True, 'data': cached['data'], 'cache_hit': True, 'version': APP_VERSION})
    try:
        data = fetch_recent_issues(code, market)
        _issue_cache[key] = {'ts': now, 'data': data}
        return jsonify({'ok': True, 'data': data, 'cache_hit': False, 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500


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
