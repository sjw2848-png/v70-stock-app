import concurrent.futures
import contextlib
import io
import math
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import FinanceDataReader as fdr

# ---------------------------------------------------------------------
# V70 SAFE WEB ENGINE
# - V69 core ideas retained
# - no fake after-hours data
# - US technical comparisons stay in native USD
# - position size uses BOTH trade budget and risk-per-trade
# ---------------------------------------------------------------------

GRADE_THRESHOLDS = {
    'S_momentum': 80, 'S_kelly': 0.10, 'S_rrr': 2.0,
    'A_momentum': 70, 'A_kelly': 0.05, 'A_rrr': 1.5,
    'B_momentum': 55, 'B_kelly': 0.0,
}

KR_CURATED = {
    '290690': {'name': '소룩스', 'theme': '아리바이오 합병/무증', 'target_pct': 12.0, 'prob': 79, 'tech': '상따매매'},
    '038110': {'name': '에코플라스틱', 'theme': '차량 경량화 범퍼', 'target_pct': 9.0, 'prob': 77, 'tech': '눌림목매매'},
    '205470': {'name': '휴마시스', 'theme': '체외진단/방역', 'target_pct': 8.0, 'prob': 60, 'tech': '뉴스매매'},
    '001470': {'name': '삼부토건', 'theme': '건설 재건/저가주 수급', 'target_pct': 12.0, 'prob': 65, 'tech': '추세매매'},
    '010140': {'name': '삼성중공업', 'theme': 'K-조선 대규모 수주랠리', 'target_pct': 7.0, 'prob': 84, 'tech': '돌파매매'},
    '000270': {'name': '기아', 'theme': '주주환원/밸류업 대장주', 'target_pct': 4.0, 'prob': 87, 'tech': '종가매매'},
    '042700': {'name': '한미반도체', 'theme': 'HBM TC본더 공급망', 'target_pct': 8.0, 'prob': 89, 'tech': '돌파매매'},
    '196170': {'name': '알테오젠', 'theme': '바이오/기술이전 모멘텀', 'target_pct': 10.0, 'prob': 83, 'tech': '눌림목매매'},
    '000660': {'name': 'SK하이닉스', 'theme': 'HBM/메모리', 'target_pct': 5.0, 'prob': 88, 'tech': '수급매매'},
    '005930': {'name': '삼성전자', 'theme': '반도체/메모리', 'target_pct': 4.0, 'prob': 85, 'tech': '지지라인매매'},
    '267260': {'name': 'HD현대일렉트릭', 'theme': '전력기기/데이터센터', 'target_pct': 8.0, 'prob': 88, 'tech': '돌파매매'},
}

US_CURATED = {
    'NVDA': {'name': '엔비디아', 'theme': 'AI 가속기', 'target_pct': 5.0, 'prob': 89, 'tech': '돌파매매'},
    'TSLA': {'name': '테슬라', 'theme': 'EV/FSD/로보택시', 'target_pct': 7.0, 'prob': 82, 'tech': '뉴스매매'},
    'AAPL': {'name': '애플', 'theme': '소비자 IT/AI 생태계', 'target_pct': 4.0, 'prob': 86, 'tech': '지지라인매매'},
    'MSFT': {'name': '마이크로소프트', 'theme': 'AI/클라우드', 'target_pct': 4.0, 'prob': 88, 'tech': '수급매매'},
    'PLTR': {'name': '팔란티어', 'theme': 'AI 소프트웨어', 'target_pct': 8.0, 'prob': 79, 'tech': '뉴스매매'},
}

KR_ETFS = {
    '069500': {'name': 'KODEX 200', 'theme': '코스피200 지수 추종', 'target_pct': 5.0, 'prob': 85, 'tech': '눌림목매매'},
    '232080': {'name': 'TIGER 코스닥150', 'theme': '코스닥150 지수 추종', 'target_pct': 7.0, 'prob': 75, 'tech': '추세매매'},
    '213610': {'name': 'KODEX 고배당주', 'theme': '배당/가치', 'target_pct': 4.0, 'prob': 90, 'tech': '추세매매'},
    '360750': {'name': 'TIGER 미국S&P500', 'theme': 'S&P500 국내상장 ETF', 'target_pct': 5.5, 'prob': 87, 'tech': '추세매매'},
    '133690': {'name': 'TIGER 미국나스닥100', 'theme': '나스닥100 국내상장 ETF', 'target_pct': 8.0, 'prob': 86, 'tech': '돌파매매'},
}

SECTOR_KEYWORDS = {
    '반도체/AI': ['반도체', 'HBM', 'D램', '엔비디아', 'NVDA', 'AI', '인공지능', '클라우드', 'MSFT', 'PLTR'],
    '2차전지/EV': ['2차전지', '배터리', '리튬', '전기차', '테슬라', 'TSLA'],
    '방산/조선': ['방산', '조선', '현대로템', '삼성중공업', '한화오션'],
    '전력/원전': ['원전', 'SMR', '전력', '데이터센터', 'HD현대일렉트릭'],
    '바이오/제약': ['바이오', '신약', 'FDA', '임상', '알테오젠', 'HLB'],
    '자동차': ['자동차', '현대차', '기아', 'EV'],
    '금융': ['금융', '은행', '보험', '카드'],
    '기타': [],
}

_KRX_CACHE = None


def _safe_float(v, default=0.0):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(round(float(v)))
    except Exception:
        return default


def get_usdkrw_rate(fallback=1400.0):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fx = yf.download('KRW=X', period='5d', progress=False, auto_adjust=False, threads=False)
        if fx is not None and not fx.empty:
            if isinstance(fx.columns, pd.MultiIndex):
                fx.columns = fx.columns.droplevel(1)
            val = float(fx['Close'].dropna().iloc[-1])
            if 900 <= val <= 2500:
                return val
    except Exception:
        pass
    return float(fallback)


def calc_atr(df, period=14):
    if len(df) < period + 1:
        return None
    prev_close = df['Close'].shift(1)
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - prev_close).abs(),
        (df['Low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return None if pd.isna(val) else float(val)


def calc_vwap_proxy(df, days=5):
    if len(df) < days or 'Volume' not in df.columns:
        return None
    recent = df.tail(days)
    vol = recent['Volume'].sum()
    if vol <= 0:
        return None
    tp = (recent['High'] + recent['Low'] + recent['Close']) / 3
    return float((tp * recent['Volume']).sum() / vol)


def calc_rvol(df, period=20):
    if len(df) < period + 1 or 'Volume' not in df.columns:
        return None
    avg = df['Volume'].iloc[-period-1:-1].mean()
    cur = df['Volume'].iloc[-1]
    return float(cur / avg) if avg and avg > 0 else None


def calc_pivots(df):
    if len(df) < 2:
        return None, None, None
    prev = df.iloc[-2]
    pp = (prev['High'] + prev['Low'] + prev['Close']) / 3
    return float(2 * pp - prev['High']), float(pp), float(2 * pp - prev['Low'])


def calc_ma(df):
    out = {}
    for p in (5, 20, 60, 120):
        if len(df) >= p:
            v = df['Close'].rolling(p).mean().iloc[-1]
            if not pd.isna(v):
                out[f'ma{p}'] = float(v)
    return out


def calc_rsi(df, period=14):
    if len(df) < period + 1:
        return None
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    g = _safe_float(gain.iloc[-1], 0.0)
    l = _safe_float(loss.iloc[-1], 0.0)
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return float(100 - 100 / (1 + g / l))


def calc_macd(df):
    if len(df) < 26:
        return False
    e12 = df['Close'].ewm(span=12, adjust=False).mean()
    e26 = df['Close'].ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig = macd.ewm(span=9, adjust=False).mean()
    return bool(macd.iloc[-1] > sig.iloc[-1])


def calc_bb_break(df, period=20):
    if len(df) < period:
        return False
    ma = df['Close'].rolling(period).mean()
    std = df['Close'].rolling(period).std()
    return bool(df['Close'].iloc[-1] >= (ma + 2 * std).iloc[-1])


def calc_return(df, days):
    if len(df) < days + 1:
        return 0.0
    return float((df['Close'].iloc[-1] / df['Close'].iloc[-days-1] - 1) * 100)


def calc_momentum(df, assumed_prob):
    if len(df) < 20:
        return float(assumed_prob)
    r5, r20 = calc_return(df, 5), calc_return(df, 20)
    v5 = df['Volume'].iloc[-5:].mean() if 'Volume' in df.columns else 1
    v20 = df['Volume'].iloc[-20:].mean() if 'Volume' in df.columns else 1
    vr = v5 / v20 if v20 and v20 > 0 else 1
    score = assumed_prob * 0.5 + np.clip(r5 * 2, -15, 15) + np.clip(r20 * 0.5, -10, 10) + np.clip((vr - 1) * 10, -10, 15)
    return float(np.clip(score, 0, 100))


def classify_sector(name, theme):
    text = f'{name} {theme}'.upper()
    best, best_score = '기타', 0
    for sec, kws in SECTOR_KEYWORDS.items():
        score = sum(1 for kw in kws if kw.upper() in text)
        if score > best_score:
            best, best_score = sec, score
    return best


def auto_strategy(close_native, ma, rsi, rvol, high20, prev_day_change, recent5):
    ma5, ma20, ma60 = ma.get('ma5'), ma.get('ma20'), ma.get('ma60')
    if prev_day_change >= 25:
        return '상따매매', '전일 급등 종목', 0.85
    if rsi is not None and rsi >= 80:
        return '추세매매', f'RSI {rsi:.0f} 과열', 0.50
    if rsi is not None and rsi < 30 and recent5 <= -8:
        return '낙주매매', f'RSI {rsi:.0f} 과매도 + 5일 급락', 0.75
    if high20 and close_native >= high20 * 0.99 and (rvol or 1) >= 1.5:
        return '돌파매매', '20일 고점권 + 상대거래량 증가', 0.85
    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60 and close_native > ma5:
        return '추세매매', '5·20·60일선 정배열', 0.85
    if ma5 and ma20 and ma5 > ma20 and abs(close_native - ma5) / ma5 * 100 < 2 and recent5 > 3:
        return '눌림목매매', '상승 추세 중 5일선 근접', 0.75
    if ma20 and abs(close_native - ma20) / ma20 * 100 < 2:
        return '지지라인매매', '20일선 근접', 0.60
    return '수급매매', '단일 차트 신호가 약함', 0.40


def calc_grade(momentum, kelly, rrr):
    t = GRADE_THRESHOLDS
    if momentum >= t['S_momentum'] and kelly >= t['S_kelly'] and rrr >= t['S_rrr']:
        return 'S'
    if momentum >= t['A_momentum'] and kelly >= t['A_kelly'] and rrr >= t['A_rrr']:
        return 'A'
    if momentum >= t['B_momentum'] and kelly > t['B_kelly']:
        return 'B'
    return 'C'


def get_krx_mapping():
    global _KRX_CACHE
    if _KRX_CACHE is not None:
        return _KRX_CACHE
    try:
        df = fdr.StockListing('KRX')
        ccol = next((c for c in ['Code', 'Symbol', '단축코드'] if c in df.columns), df.columns[0])
        ncol = next((c for c in ['Name', '한글 종목약명', '종목명'] if c in df.columns), df.columns[1])
        _KRX_CACHE = {str(r[ncol]).strip(): str(r[ccol]).strip().replace('KRX:', '').zfill(6) for _, r in df.iterrows()}
    except Exception:
        _KRX_CACHE = {}
    return _KRX_CACHE


def load_krx_top(n=60):
    out = {}
    try:
        df = fdr.StockListing('KRX')
        mcol = next((c for c in ['Marcap', 'MarketCap', '시가총액'] if c in df.columns), None)
        ccol = next((c for c in ['Code', 'Symbol', '단축코드'] if c in df.columns), df.columns[0])
        ncol = next((c for c in ['Name', '한글 종목약명', '종목명'] if c in df.columns), df.columns[1])
        if mcol:
            df = df.sort_values(mcol, ascending=False)
        for _, r in df.iterrows():
            if len(out) >= n:
                break
            code = str(r[ccol]).strip()
            if code.startswith('KR') and len(code) >= 9:
                code = code[3:9]
            if not code.isdigit():
                continue
            code = code.zfill(6)
            name = str(r[ncol]).strip()
            if any(x in name for x in ['우B', '(우)', '스팩', '리츠', 'KODEX', 'TIGER', '인버스', '선물']):
                continue
            out[code] = {'name': name, 'theme': 'KRX 시가총액 상위', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'}
    except Exception:
        pass
    return out


def fetch_popular(max_n=20):
    out = {}
    try:
        res = requests.get('https://finance.naver.com/sise/lastsearch2.naver', headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        res.raise_for_status()
        tables = pd.read_html(io.StringIO(res.text), encoding='euc-kr')
        df = tables[1].dropna(how='all').dropna(subset=['순위', '종목명', '등락률'])
        mapping = get_krx_mapping()
        for _, row in df.iterrows():
            if len(out) >= max_n:
                break
            name = str(row['종목명']).strip()
            if name not in mapping:
                continue
            chg_text = str(row['등락률']).strip()
            try:
                chg = float(chg_text.replace('%', '').replace('+', ''))
            except Exception:
                chg = 0
            tech = '상따매매' if chg > 15 else ('돌파매매' if chg >= 0 else '낙주매매')
            out[mapping[name]] = {'name': name, 'theme': f'네이버 인기종목 ({chg_text})', 'target_pct': 4.0, 'prob': 75, 'tech': tech}
    except Exception:
        pass
    return out


def diagnostic_market():
    result = {'kospi_chg': None, 'kosdaq_chg': None, 'state': '데이터 확인 중', 'guide': '지수 데이터 확인이 필요합니다.'}
    try:
        kp = fdr.DataReader('KS11').tail(3)
        kq = fdr.DataReader('KQ11').tail(3)
        kpchg = (float(kp['Close'].iloc[-1]) / float(kp['Close'].iloc[-2]) - 1) * 100
        kqchg = (float(kq['Close'].iloc[-1]) / float(kq['Close'].iloc[-2]) - 1) * 100
        result['kospi_chg'], result['kosdaq_chg'] = kpchg, kqchg
        mn, mx = min(kpchg, kqchg), max(kpchg, kqchg)
        if mn <= -3:
            result['state'], result['guide'] = '🚨 급락장', '신규 진입을 줄이고 리스크를 낮추는 구간으로 분류합니다.'
        elif mn <= -1:
            result['state'], result['guide'] = '🔴 약세장', '돌파 추격보다 보수적인 진입이 유리한 구간으로 분류합니다.'
        elif mx >= 1.5:
            result['state'], result['guide'] = '🟢 강세장', '시장 모멘텀이 강한 구간입니다. 그래도 종목별 손절 기준은 유지하세요.'
        else:
            result['state'], result['guide'] = '🟡 혼조세', '종목별 신호 차이가 큰 구간입니다. 선별 접근이 필요합니다.'
    except Exception as e:
        result['guide'] = f'지수 수신 실패: {str(e)[:80]}'
    return result


def _load_price(code, market):
    if market == 'US':
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            df = yf.download(code, period='6mo', interval='1d', progress=False, auto_adjust=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        return df
    # KR / KR_ETF
    try:
        df = fdr.DataReader(code)
        if df is not None and not df.empty:
            return df.tail(180)
    except Exception:
        pass
    # fallback yfinance KS/KQ
    for suffix in ('.KS', '.KQ'):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                df = yf.download(f'{code}{suffix}', period='6mo', progress=False, auto_adjust=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
    return pd.DataFrame()


def analyze_one(code, info, market, settings, fx, market_state):
    try:
        df = _load_price(code, market)
        if df is None or df.empty:
            raise ValueError('가격 데이터 수신 실패')
        df = df.dropna(subset=['Close'])
        if len(df) < 20:
            raise ValueError('분석에 필요한 일봉 부족')

        close = float(df['Close'].iloc[-1])  # native currency
        open_p = _safe_float(df['Open'].iloc[-1], close)
        prev_close = _safe_float(df['Close'].iloc[-2], close)
        prev2_close = _safe_float(df['Close'].iloc[-3], prev_close) if len(df) >= 3 else prev_close
        high20 = float(df['High'].tail(20).max())
        prev_day_chg = (prev_close / prev2_close - 1) * 100 if prev2_close else 0
        gap_pct = (open_p / prev_close - 1) * 100 if prev_close else 0
        recent5 = calc_return(df, 5)

        atr = calc_atr(df)
        atr_pct = atr / close * 100 if atr and close else 3.0
        rvol = calc_rvol(df) or 1.0
        ma = calc_ma(df)
        rsi = calc_rsi(df)
        macd_bull = calc_macd(df)
        bb_break = calc_bb_break(df)
        vwap_proxy = calc_vwap_proxy(df) or close
        s1, pp, r1 = calc_pivots(df)
        s1, pp, r1 = s1 or close * .97, pp or close, r1 or close * 1.03

        prob = float(info.get('prob', 70))
        momentum = calc_momentum(df, prob)
        if macd_bull:
            momentum += 10
        if bb_break:
            momentum += 10
        if rvol >= 3:
            momentum += 15
        elif rvol >= 2:
            momentum += 10
        momentum = min(100.0, momentum)
        if '약세' in market_state or '급락' in market_state:
            momentum = max(0.0, momentum - 5)

        auto_tech, reason, confidence = auto_strategy(close, ma, rsi, rvol, high20, prev_day_chg, recent5)
        tech = auto_tech if confidence >= 0.5 else info.get('tech', auto_tech)

        target_pct = float(info.get('target_pct', 4.0))
        trust = settings.get('trust_mode', 'balanced')
        if trust in ('balanced', 'aggressive') and atr_pct >= 5:
            target_pct = max(target_pct, atr_pct * (1.4 if trust == 'balanced' else 1.7))
        if macd_bull or bb_break:
            target_pct *= 1.15

        stop_pct = max(float(settings.get('stop_pct', 3.0)), atr_pct * 1.5)
        rrr = target_pct / stop_pct if stop_pct > 0 else 0
        win_rate = prob / 100.0
        kelly = max(0.0, min(0.20, (win_rate - (1 - win_rate) / max(rrr, 0.01)) * 0.5))
        grade = calc_grade(momentum, kelly, rrr)

        if '급락' in market_state:
            grade = {'S': 'A', 'A': 'B', 'B': 'C'}.get(grade, grade)
        min_rrr = float(settings.get('min_rrr', 1.5))
        if rrr < min_rrr:
            grade = 'C'

        if tech in ('돌파매매', '상따매매', '뉴스매매'):
            entry = close
        elif tech in ('눌림목매매', '지지라인매매', '낙주매매'):
            entry = close * 0.985
        else:
            entry = close * 0.99

        target1 = entry * (1 + target_pct / 100)
        if macd_bull or bb_break:
            target1 = max(target1, r1)
        target2 = target1 * 1.05
        target3 = target2 * 1.05
        stop1 = entry * (1 - stop_pct / 100)
        stop2 = entry * (1 - stop_pct * 1.5 / 100)
        stop3 = entry * (1 - stop_pct * 2.0 / 100)

        currency = 'USD' if market == 'US' else 'KRW'
        krw_mult = fx if market == 'US' else 1.0
        close_krw = close * krw_mult
        entry_krw = entry * krw_mult
        risk_per_share_krw = max((entry - stop1) * krw_mult, 1)

        total_budget = max(int(settings.get('budget', 5_000_000)), 0)
        trade_budget = max(int(settings.get('trade_budget', 300_000)), 0)
        risk_pct = max(float(settings.get('risk_pct', 1.0)), 0.0) / 100.0
        max_loss = total_budget * risk_pct

        qty_budget = int(trade_budget // entry_krw) if entry_krw > 0 else 0
        qty_risk = int(max_loss // risk_per_share_krw) if risk_per_share_krw > 0 else 0
        qty = min(qty_budget, qty_risk)
        if grade == 'C' or kelly <= 0:
            qty = 0

        invested = qty * entry_krw
        expected_profit = qty * (target1 - entry) * krw_mult
        expected_loss = qty * (stop1 - entry) * krw_mult

        vol = _safe_float(df['Volume'].iloc[-1], 0)
        trading_value_krw = close * vol * krw_mult
        sector = classify_sector(info.get('name', code), info.get('theme', ''))

        decision = {
            'S': '최우선 후보', 'A': '강한 후보', 'B': '분할 검토', 'C': '관망'
        }[grade]

        held_price = _safe_float(info.get('my_price'), 0)
        held_pnl = None
        if held_price > 0:
            held_pnl = (close_krw / held_price - 1) * 100

        return {
            'ok': True,
            'code': code,
            'name': info.get('name', code),
            'market': market,
            'category': 'US' if market == 'US' else ('ETF' if market == 'ETF' else 'KR'),
            'theme': info.get('theme', ''),
            'sector': sector,
            'grade': grade,
            'decision': decision,
            'price_native': round(close, 4),
            'currency': currency,
            'price_krw': _safe_int(close_krw),
            'entry_krw': _safe_int(entry_krw),
            'target1_krw': _safe_int(target1 * krw_mult),
            'target2_krw': _safe_int(target2 * krw_mult),
            'target3_krw': _safe_int(target3 * krw_mult),
            'stop1_krw': _safe_int(stop1 * krw_mult),
            'stop2_krw': _safe_int(stop2 * krw_mult),
            'stop3_krw': _safe_int(stop3 * krw_mult),
            'qty': qty,
            'invested_krw': _safe_int(invested),
            'expected_profit_krw': _safe_int(expected_profit),
            'expected_loss_krw': _safe_int(expected_loss),
            'momentum': round(momentum, 1),
            'rrr': round(rrr, 2),
            'kelly_pct': round(kelly * 100, 1),
            'assumed_win_rate': round(prob, 1),
            'atr_pct': round(atr_pct, 2),
            'rvol': round(rvol, 2),
            'rsi': None if rsi is None else round(rsi, 1),
            'gap_pct': round(gap_pct, 2),
            'recent5_pct': round(recent5, 2),
            'ma5_krw': _safe_int(ma.get('ma5', 0) * krw_mult),
            'ma20_krw': _safe_int(ma.get('ma20', 0) * krw_mult),
            'vwap_proxy_krw': _safe_int(vwap_proxy * krw_mult),
            'pivot_s1_krw': _safe_int(s1 * krw_mult),
            'pivot_pp_krw': _safe_int(pp * krw_mult),
            'pivot_r1_krw': _safe_int(r1 * krw_mult),
            'macd_bull': macd_bull,
            'bb_break': bb_break,
            'trading_value_krw': _safe_int(trading_value_krw),
            'tech': tech,
            'strategy_reason': reason,
            'strategy_confidence': round(confidence * 100, 0),
            'held_price_krw': _safe_int(held_price) if held_price > 0 else None,
            'held_pnl_pct': None if held_pnl is None else round(held_pnl, 2),
            'after_hours': None,
            'data_note': '일봉 기반 기술지표. 시간외 가격은 제공하지 않음.',
        }
    except Exception as e:
        return {
            'ok': False, 'code': code, 'name': info.get('name', code), 'market': market,
            'category': 'US' if market == 'US' else ('ETF' if market == 'ETF' else 'KR'),
            'grade': 'C', 'decision': '데이터 오류', 'error': str(e)[:120], 'theme': info.get('theme', '')
        }


def parse_held(text: str):
    result = []
    if not text:
        return result
    mapping = get_krx_mapping()
    for raw in text.split(','):
        item = raw.strip()
        if not item:
            continue
        parts = item.split(':', 1)
        key = parts[0].strip()
        price = _safe_float(parts[1].replace(',', '').replace('원', '').strip(), 0) if len(parts) > 1 else 0
        code, name = '', ''
        if key.isdigit():
            code = key.zfill(6)
            # reverse cache best-effort
            name = next((n for n, c in mapping.items() if c == code), code)
        elif key in mapping:
            code, name = mapping[key], key
        if code:
            result.append((code, {'name': name, 'theme': '보유/검색 종목', 'target_pct': 4.0, 'prob': 50, 'tech': '추세매매', 'my_price': price}))
    return result


def build_universe(mode='curated', top_n=60):
    if mode == 'popular':
        kr = fetch_popular(20)
    elif mode == 'top':
        kr = load_krx_top(top_n)
    elif mode == 'mixed':
        kr = {**load_krx_top(min(top_n, 100)), **fetch_popular(20), **KR_CURATED}
    else:
        kr = dict(KR_CURATED)
    return kr


def analyze(settings: Dict[str, Any]):
    started = time.time()
    fx = get_usdkrw_rate()
    market = diagnostic_market()
    mode = settings.get('mode', 'curated')
    top_n = max(10, min(int(settings.get('top_n', 60)), 150))
    kr = build_universe(mode, top_n)
    us = dict(US_CURATED)
    etf = dict(KR_ETFS)
    held = parse_held(settings.get('held', ''))

    tasks: List[Tuple[str, Dict[str, Any], str]] = []
    # Held first so they always appear.
    for c, i in held:
        tasks.append((c, i, 'KR'))
    for c, i in kr.items():
        tasks.append((c, i, 'KR'))
    for c, i in us.items():
        tasks.append((c, i, 'US'))
    for c, i in etf.items():
        tasks.append((c, i, 'ETF'))

    # de-duplicate by market+code, held version takes priority
    dedup = {}
    for c, i, m in tasks:
        key = (m, c)
        if key not in dedup or '보유/검색' in i.get('theme', ''):
            dedup[key] = (c, i, m)
    tasks = list(dedup.values())

    results = []
    workers = 4 if len(tasks) < 80 else 6
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(analyze_one, c, i, m, settings, fx, market['state']) for c, i, m in tasks]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    grade_rank = {'S': 4, 'A': 3, 'B': 2, 'C': 1}
    results.sort(key=lambda x: (x.get('ok', False), grade_rank.get(x.get('grade', 'C'), 0), x.get('momentum', 0)), reverse=True)

    valid = [x for x in results if x.get('ok')]
    recommended = [x for x in valid if x.get('grade') in ('S', 'A', 'B') and x.get('qty', 0) > 0]
    summary = {
        'total': len(results),
        'valid': len(valid),
        'recommended': len(recommended),
        's_count': sum(1 for x in valid if x.get('grade') == 'S'),
        'a_count': sum(1 for x in valid if x.get('grade') == 'A'),
        'elapsed_sec': round(time.time() - started, 1),
        'usdkrw': round(fx, 2),
    }
    return {'market': market, 'summary': summary, 'results': results}
