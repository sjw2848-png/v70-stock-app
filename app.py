import hashlib
import atexit
import json
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
from engine import analyze, analyze_search, fetch_fundamentals, fetch_recent_issues

APP_VERSION = 'V70.11'
app = Flask(__name__)

_cache_lock = threading.Lock()
_cache = {}
_last_request_by_ip = {}
_fund_cache = {}
_issue_cache = {}
CACHE_TTL = max(0, int(os.environ.get('CACHE_TTL_SECONDS', '120')))
MIN_REQUEST_GAP = max(0.0, float(os.environ.get('MIN_REQUEST_GAP_SECONDS', '2')))
APP_PIN = os.environ.get('APP_PIN', '').strip()

PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.v70.pid')

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
    })


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
    defaults = {
        'budget': 5_000_000,
        'trade_budget': 300_000,
        'long_budget': 1_000_000,
        'risk_pct': 1.0,
        'stop_pct': 3.0,
        'min_rrr': 1.5,
        'trust_mode': 'balanced',
        'mode': 'curated',
        'top_n': 60,
        'held': '',
        'search_avg_price': 0,
        'search_held_qty': 0,
    }
    settings = {**defaults, **payload}
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

    defaults = {
        'budget': 5_000_000,
        'trade_budget': 300_000,
        'long_budget': 1_000_000,
        'risk_pct': 1.0,
        'stop_pct': 3.0,
        'min_rrr': 1.5,
        'trust_mode': 'balanced',
        'mode': 'curated',
        'top_n': 60,
        'held': '',
        'search_avg_price': 0,
        'search_held_qty': 0,
    }
    settings = {**defaults, **payload}
    try:
        data = analyze_search(query, settings, market_hint=market_hint)
        return jsonify({'ok': True, **data, 'generated_at': _now_iso(), 'version': APP_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300], 'version': APP_VERSION}), 500



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
