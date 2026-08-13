import concurrent.futures
import contextlib
import io
import math
import re
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import FinanceDataReader as fdr

# ---------------------------------------------------------------------
# V70.9 SAFE WEB ENGINE
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
    '반도체': ['반도체','HBM','D램','메모리','파운드리','TC본더','한미반도체','SK하이닉스','삼성전자','NVDA','AMD','TSM','AVGO'],
    'AI/소프트웨어': ['AI','인공지능','클라우드','소프트웨어','팔란티어','PLTR','MSFT','GOOGL','ORCL','CRM','META'],
    '로봇/자동화': ['로봇','자동화','레인보우로보틱스','로보티즈','현대무벡스'],
    '2차전지/EV': ['2차전지','배터리','리튬','양극재','음극재','전기차','에코프로','LG에너지솔루션','포스코퓨처엠','TSLA'],
    '자동차/부품': ['자동차','완성차','차량','현대차','기아','현대모비스','현대글로비스','에코플라스틱'],
    '조선/해운': ['조선','해운','삼성중공업','한화오션','HD한국조선해양','HMM'],
    '방산/항공우주': ['방산','전차','미사일','항공우주','한화에어로스페이스','현대로템','한국항공우주'],
    '전력/원전': ['원전','SMR','전력','변압기','전력기기','두산에너빌리티','HD현대일렉트릭','LS ELECTRIC','효성중공업'],
    '바이오/제약': ['바이오','신약','FDA','임상','의약','알테오젠','HLB','셀트리온','삼성바이오로직스','LLY','MRK'],
    '의료기기/뷰티': ['의료기기','미용','뷰티','클래시스','파마리서치','휴젤','에이피알'],
    '금융/보험': ['금융','은행','보험','증권','KB금융','신한지주','하나금융','우리금융','삼성생명','삼성화재','JPM','BAC'],
    '통신/네트워크': ['통신','5G','네트워크','SK텔레콤','KT','LG유플러스','CSCO'],
    '식품/음료': ['식품','음식료','음료','라면','담배','삼양식품','KT&G','KO','PEP','MCD'],
    '유통/소비재': ['유통','소비재','리테일','코스트코','월마트','COST','WMT'],
    '화학/철강/소재': ['화학','철강','소재','POSCO','LG화학','후성','동진쎄미켐'],
    '건설/인프라': ['건설','인프라','SOC','현대건설','삼부토건','대우건설'],
    '에너지/정유/가스': ['에너지','정유','원유','가스','S-Oil','SK이노베이션','XOM','CVX'],
    '게임/엔터/미디어': ['게임','엔터','미디어','넷플릭스','디즈니','NFLX','DIS'],
    '여행/항공': ['여행','항공','LCC','보잉','BA','우버','UBER'],
    '양자/딥테크': ['양자','quantum','IONQ','QUBT','RGTI'],
    '기타': [],
}

SECTOR_MAJOR_MAP = {
    '반도체':'IT/테크','AI/소프트웨어':'IT/테크','로봇/자동화':'IT/테크','통신/네트워크':'IT/테크','양자/딥테크':'IT/테크',
    '2차전지/EV':'모빌리티/소재','자동차/부품':'모빌리티/소재','화학/철강/소재':'모빌리티/소재',
    '조선/해운':'산업재','방산/항공우주':'산업재','전력/원전':'산업재','건설/인프라':'산업재',
    '바이오/제약':'헬스케어','의료기기/뷰티':'헬스케어',
    '금융/보험':'금융','식품/음료':'소비재','유통/소비재':'소비재','게임/엔터/미디어':'콘텐츠','여행/항공':'서비스',
    '에너지/정유/가스':'에너지','기타':'기타',
}

KNOWN_THEME_TAGS = {
    '삼성전자':['메모리','HBM','AI 데이터센터'], 'SK하이닉스':['HBM','메모리','AI 데이터센터'], '한미반도체':['HBM','TC본더'],
    '기아':['완성차','EV','주주환원'], '현대차':['완성차','EV','하이브리드'], '현대모비스':['자동차부품','전동화'],
    '삼성중공업':['LNG선','조선수주'], '한화오션':['조선','방산'], '현대로템':['방산','철도'],
    'HD현대일렉트릭':['변압기','전력기기','AI 데이터센터'], '두산에너빌리티':['원전','SMR','가스터빈'],
    '알테오젠':['바이오','기술이전'], '셀트리온':['바이오시밀러'], '삼성바이오로직스':['CDMO','바이오'],
    'SK텔레콤':['통신','AI'], 'KT':['통신','IDC'], 'KT&G':['담배','배당'],
    'NVDA':['AI GPU','데이터센터','반도체'], 'MSFT':['클라우드','AI','소프트웨어'], 'PLTR':['AI 플랫폼','데이터분석'],
    'TSLA':['EV','에너지저장','자율주행'], 'AAPL':['소비자IT','서비스'],
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


def classify_theme_detail(name, theme='', source_sector=''):
    text = f'{name} {theme} {source_sector}'.upper()
    best, best_score = '기타', 0
    for sec, kws in SECTOR_KEYWORDS.items():
        if sec == '기타':
            continue
        score = sum(2 if kw.upper() == str(name).upper() else 1 for kw in kws if kw.upper() in text)
        if score > best_score:
            best, best_score = sec, score
    major = SECTOR_MAJOR_MAP.get(best, '기타')
    tags = []
    for key, vals in KNOWN_THEME_TAGS.items():
        if key.upper() in text:
            tags.extend(vals)
    for token in re.split(r'[/,·\s]+', str(theme)):
        token = token.strip()
        if 2 <= len(token) <= 18 and token not in ('KRX','시총','상위','자동','보유','검색','종목'):
            tags.append(token)
    uniq=[]
    for x in tags:
        if x and x not in uniq:
            uniq.append(x)
    if not uniq and best != '기타':
        uniq=[best]
    return {'major': major, 'industry': best, 'theme_tags': uniq[:4]}


def classify_sector(name, theme, source_sector=''):
    return classify_theme_detail(name, theme, source_sector)['industry']


def build_sector_analytics(results):
    valid=[x for x in results if x.get('ok')]
    groups={}
    for x in valid:
        sec=x.get('sector') or '기타'
        groups.setdefault(sec,[]).append(x)
    ranking=[]
    for sec, items in groups.items():
        n=len(items)
        if not n: continue
        avg_m=float(np.mean([_safe_float(x.get('momentum'),0) for x in items]))
        avg_r5=float(np.mean([_safe_float(x.get('recent5_pct'),0) for x in items]))
        avg_rvol=float(np.mean([_safe_float(x.get('rvol'),1) for x in items]))
        breadth=100.0*sum(1 for x in items if x.get('above_ma20'))/n
        buy_count=sum(1 for x in items if x.get('grade') in ('S','A','B') and x.get('qty',0)>0 and not x.get('hard_block'))
        long_count=sum(1 for x in items if x.get('long_quality_candidate'))
        turnover=sum(_safe_float(x.get('trading_value_krw'),0) for x in items)
        score=np.clip(avg_m*0.45 + np.clip(avg_r5+5,0,15)*1.4 + np.clip(avg_rvol,0,3)*8 + breadth*0.20, 0, 100)
        label='🔥 강함' if score>=72 else ('🟡 보통' if score>=52 else '⚪ 약함')
        ranking.append({'sector':sec,'major':SECTOR_MAJOR_MAP.get(sec,'기타'),'score':round(float(score),1),'label':label,'count':n,'avg_momentum':round(avg_m,1),'avg_5d':round(avg_r5,2),'avg_rvol':round(avg_rvol,2),'breadth_pct':round(breadth,1),'buy_count':buy_count,'long_quality_count':long_count,'trading_value_krw':_safe_int(turnover)})
        leader_sorted=sorted(items,key=lambda x: (_safe_float(x.get('momentum'),0)+min(_safe_float(x.get('rvol'),1),4)*7+_safe_float(x.get('recent5_pct'),0)+_safe_float(x.get('attention_score'),0)*0.25),reverse=True)
        for idx,x in enumerate(leader_sorted):
            x['sector_rank']=idx+1
            x['sector_role']='👑 분석군 대장 후보' if idx==0 and len(leader_sorted)>=2 else ('🥈 분석군 2등 후보' if idx==1 and len(leader_sorted)>=3 else '동행/후발')
    ranking.sort(key=lambda x:x['score'], reverse=True)
    held=[x for x in valid if x.get('held_price_krw') and x.get('held_qty')]
    total=sum(_safe_float(x.get('price_krw'),0)*_safe_float(x.get('held_qty'),0) for x in held)
    alloc=[]
    if total>0:
        by={}
        for x in held:
            v=_safe_float(x.get('price_krw'),0)*_safe_float(x.get('held_qty'),0)
            by[x.get('sector') or '기타']=by.get(x.get('sector') or '기타',0)+v
        alloc=[{'sector':k,'value_krw':_safe_int(v),'weight_pct':round(v/total*100,1)} for k,v in by.items()]
        alloc.sort(key=lambda x:x['weight_pct'], reverse=True)
    warnings=[]
    for a in alloc:
        if a['weight_pct']>=60: warnings.append(f"🚨 {a['sector']} 비중 {a['weight_pct']}% — 한 섹터에 지나치게 집중")
        elif a['weight_pct']>=40: warnings.append(f"⚠️ {a['sector']} 비중 {a['weight_pct']}% — 섹터 쏠림 주의")
    return {'top_sectors':ranking[:8],'holding_allocation':alloc,'concentration_warnings':warnings,'note':'섹터 강도와 대장 표시는 현재 분석군 내부의 상대 비교이며 시장 전체 순위를 의미하지 않습니다.'}


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
        scol = next((c for c in ['Sector','Industry','업종','업종명'] if c in df.columns), None)
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



def get_trade_timing(tech: str, market: str):
    """Beginner-facing entry/exit timing. Times are market-local clocks."""
    is_us = market == 'US'
    if is_us:
        buy_map = {
            '돌파매매': ('09:35~10:15 ET', '장 시작 5분은 피하고 거래량이 붙은 돌파를 확인'),
            '눌림목매매': ('10:15~11:30 ET', '초반 급등락이 진정된 뒤 5일선·지지구간 눌림 확인'),
            '지지라인매매': ('10:30~12:00 ET', '지지선 이탈 없이 반등 캔들이 나온 뒤'),
            '낙주매매': ('10:00~11:00 ET', '급락이 멈추고 저점 재이탈이 없는지 확인'),
            '종가매매': ('15:30~15:50 ET', '마감 직전까지 강도가 유지될 때만'),
            '상따매매': ('초보자 비추천', '미국장은 상한가 제도가 없어 고변동 추격매수로 간주'),
            '뉴스매매': ('09:35~10:15 ET', '뉴스 직후 첫 급등을 쫓지 말고 거래량 확인'),
            '수급매매': ('10:00~11:30 ET', '개장 직후 노이즈가 줄고 추세가 유지될 때'),
            '추세매매': ('10:00~11:30 ET', '초반 방향이 확인된 뒤 추세 지속 시'),
        }
        sell_default = '1차 목표 도달 시 50% 익절 · 손절가 이탈 즉시 정리 · 15:45 ET 이후 신규진입 금지'
    else:
        buy_map = {
            '돌파매매': ('09:05~09:30', '시초 5분은 피하고 거래량 동반 돌파를 확인'),
            '눌림목매매': ('10:00~11:30 / 13:00~14:00', '오전 급등락 후 지지구간 눌림 확인'),
            '지지라인매매': ('10:30~11:30 / 13:30~14:20', '지지선 이탈 없이 반등 확인 후'),
            '낙주매매': ('09:30~10:30 / 14:00~14:30', '투매가 멈추고 저점 재이탈이 없는지 확인'),
            '종가매매': ('15:10~15:20', '마감 직전까지 강도가 유지될 때만'),
            '상따매매': ('13:30~14:30', '초고위험. 상한가 안착 여부 확인 전 추격 금지'),
            '뉴스매매': ('09:05~09:30', '뉴스 직후 첫 급등을 쫓지 말고 거래량 확인'),
            '수급매매': ('09:30~11:00 / 13:30~14:30', '거래대금과 추세가 함께 유지될 때'),
            '추세매매': ('09:30~11:00 / 13:30~14:30', '시초 변동 후 방향이 확인된 뒤'),
        }
        sell_default = '1차 목표 도달 시 50% 익절 · 손절가 이탈 즉시 정리 · 14:30 이후 신규진입 자제'
    buy_time, buy_rule = buy_map.get(tech, buy_map['추세매매'])
    time_stop = {
        '돌파매매': '진입 후 30~60분 안에 탄력이 없으면 비중 축소',
        '눌림목매매': '진입 후 2~3시간 동안 반등이 없으면 재평가',
        '지지라인매매': '지지 반등 실패 시 즉시 손절 기준 우선',
        '낙주매매': '저점 재이탈 시 기다리지 말고 손절',
        '종가매매': '다음 거래일 초반까지 시나리오가 틀리면 정리',
        '뉴스매매': '10~30분 내 반응 없으면 재료 약화로 판단',
        '상따매매': '초보자는 보유시간을 짧게, 풀리면 즉시 재평가',
    }.get(tech, '당일 추세가 꺾이거나 손절가 이탈 시 정리')
    return {
        'buy_time': buy_time,
        'buy_rule': buy_rule,
        'sell_plan': sell_default,
        'time_stop': time_stop,
        'time_zone_note': '미국 현지시간(ET)' if is_us else '한국 거래시간(KST)',
    }


def _yahoo_symbol_candidates(code: str, market: str):
    if market == 'US':
        return [code]
    return [f'{code}.KS', f'{code}.KQ']



def build_attention_signal(close, high20, rvol, trading_value_krw, recent5, bb_break, theme=''):
    """Volume/price-interest heat. Does not claim causation or insider activity."""
    score = 0.0
    reasons = []
    if rvol >= 3.0:
        score += 35; reasons.append(f'평소 대비 거래량 {rvol:.1f}배')
    elif rvol >= 2.0:
        score += 25; reasons.append(f'평소 대비 거래량 {rvol:.1f}배')
    elif rvol >= 1.5:
        score += 15; reasons.append(f'거래량 증가 {rvol:.1f}배')
    if trading_value_krw >= 100_000_000_000:
        score += 25; reasons.append('거래대금 1,000억원 이상')
    elif trading_value_krw >= 50_000_000_000:
        score += 18; reasons.append('거래대금 500억원 이상')
    elif trading_value_krw >= 10_000_000_000:
        score += 8; reasons.append('거래대금 100억원 이상')
    if high20 and close >= high20 * 0.99:
        score += 15; reasons.append('20일 고점권')
    if bb_break:
        score += 10; reasons.append('볼린저 상단 돌파')
    if recent5 >= 8:
        score += 8; reasons.append(f'최근 5일 +{recent5:.1f}%')
    if '인기종목' in str(theme):
        score += 15; reasons.append('실시간 인기종목 유입')
    score = min(100.0, score)
    if score >= 70:
        label = '🚀 관심 폭발'
    elif score >= 45:
        label = '🔥 관심 급증'
    elif score >= 25:
        label = '👀 관심 증가'
    else:
        label = '⚪ 관심 보통'
    return round(score,1), label, reasons[:5]


def classify_news_quality(title: str):
    """Headline-only risk/positive taxonomy. It never treats a headline as verified causation."""
    t = str(title or '').lower()
    positive = {
        '수주': 3, '계약': 3, '공급': 2, '승인': 3, 'fda': 3, '흑자': 3, '증익': 2,
        '실적 개선': 3, '매출 증가': 2, '파트너십': 2, 'partnership': 2, 'contract': 3,
        'order': 2, 'approval': 3, 'guidance raised': 3, 'beat': 2, 'launch': 1,
    }
    risky = {
        '유상증자': -4, '증자': -2, 'cb': -3, '전환사채': -3, '소송': -3, '회계': -3,
        '감사의견': -4, '상장폐지': -5, '횡령': -5, '배임': -5, '리콜': -3, '적자': -3,
        '하향': -2, 'downgrade': -2, 'offering': -3, 'dilution': -4, 'lawsuit': -3,
        'investigation': -3, 'miss': -2, 'guidance cut': -4, 'recall': -3,
    }
    score = 0
    tags = []
    for k, v in positive.items():
        if k in t:
            score += v; tags.append(f'긍정:{k}')
    for k, v in risky.items():
        if k in t:
            score += v; tags.append(f'주의:{k}')
    if score >= 4: label = '🟢 긍정 이슈 우세'
    elif score <= -4: label = '🔴 위험 이슈 주의'
    elif score > 0: label = '🟡 긍정 키워드 일부'
    elif score < 0: label = '🟠 위험 키워드 일부'
    else: label = '⚪ 중립/분류 어려움'
    return score, label, tags[:4]


def fetch_recent_issues(code: str, market: str, limit: int = 6):
    """Best-effort recent headlines from Yahoo Finance with headline-quality classification."""
    last_error = None
    for symbol in _yahoo_symbol_candidates(code, market):
        try:
            raw = yf.Ticker(symbol).news or []
            items = []
            total_score = 0
            pos_count = risk_count = 0
            for n in raw[:max(limit*2, 10)]:
                if not isinstance(n, dict):
                    continue
                c = n.get('content') if isinstance(n.get('content'), dict) else n
                title = c.get('title') or n.get('title')
                provider = c.get('provider') or {}
                publisher = provider.get('displayName') if isinstance(provider, dict) else None
                publisher = publisher or c.get('publisher') or n.get('publisher') or 'Yahoo Finance'
                click = c.get('clickThroughUrl') or c.get('canonicalUrl') or {}
                link = click.get('url') if isinstance(click, dict) else click
                link = link or c.get('link') or n.get('link')
                published = c.get('pubDate') or c.get('displayTime') or n.get('providerPublishTime')
                if not title:
                    continue
                score, quality, tags = classify_news_quality(title)
                total_score += score
                if score > 0: pos_count += 1
                if score < 0: risk_count += 1
                items.append({'title': str(title), 'publisher': str(publisher), 'link': link, 'published': published,
                              'quality_score': score, 'quality_label': quality, 'tags': tags})
                if len(items) >= limit:
                    break
            if items:
                if total_score >= 5: overall = '🟢 최근 이슈 긍정 우세'
                elif total_score <= -5: overall = '🔴 최근 이슈 위험 우세'
                elif risk_count and pos_count: overall = '🟡 호재·악재 혼재'
                elif pos_count: overall = '🟡 긍정 이슈 일부'
                elif risk_count: overall = '🟠 위험 이슈 일부'
                else: overall = '⚪ 중립 이슈'
                return {'symbol': symbol, 'items': items, 'count': len(items), 'keyword_hits': pos_count+risk_count,
                        'positive_count': pos_count, 'risk_count': risk_count, 'news_score': total_score,
                        'news_label': overall, 'source': 'Yahoo Finance', 'error': None,
                        'note': '제목 키워드 분류이며 기사 사실관계·영향을 확정하지 않습니다.'}
        except Exception as e:
            last_error = str(e)
    return {'symbol': code, 'items': [], 'count': 0, 'keyword_hits': 0, 'positive_count':0, 'risk_count':0,
            'news_score':0, 'news_label':'최근 이슈 데이터 없음', 'source': 'Yahoo Finance',
            'note':'제목 기반 분류', 'error': ('최근 이슈 데이터 없음' + (f': {last_error[:100]}' if last_error else ''))}

def build_flow_proxy(df):
    """Price-volume accumulation proxy. This is NOT foreign/institutional investor flow."""
    if len(df) < 10 or 'Volume' not in df.columns:
        return {'score':50.0,'label':'수급 프록시 데이터 부족','reason':'가격·거래량 데이터 부족','note':'외국인/기관 실제 순매수 데이터가 아님'}
    d=df.tail(10).copy()
    ret=d['Close'].pct_change().fillna(0)
    vol=d['Volume'].fillna(0)
    base=max(float(vol.mean()),1.0)
    signed=float(((ret.clip(-0.08,0.08))* (vol/base)).tail(5).sum())
    upvol=float(vol.where(ret>0,0).tail(5).sum())
    dnvol=float(vol.where(ret<0,0).tail(5).sum())
    ratio=upvol/max(dnvol,1.0)
    score=max(0,min(100,50 + signed*220 + max(-15,min(15,(ratio-1)*10))))
    if score>=68: label='🟢 매수 우위 수급 프록시'
    elif score<=35: label='🔴 매도 우위 수급 프록시'
    else: label='🟡 수급 프록시 중립'
    return {'score':round(score,1),'label':label,'reason':f'최근 5일 상승거래량/하락거래량 비 {ratio:.2f}',
            'note':'가격·거래량으로 계산한 프록시이며 외국인/기관 실제 순매수 데이터가 아닙니다.'}


def build_backtest_proxy(df):
    """Lightweight historical similar-signal check, avoiding claims that it validates the full model."""
    if len(df) < 70:
        return {'trades':0,'success_rate':None,'avg_return_5d':None,'label':'백테스트 데이터 부족','note':'유사 기술신호 5거래일 후 수익률'}
    d=df.copy().tail(180)
    close=d['Close']
    ma5=close.rolling(5).mean(); ma20=close.rolling(20).mean()
    vol=d['Volume'] if 'Volume' in d.columns else pd.Series(1,index=d.index)
    vavg=vol.rolling(20).mean()
    delta=close.diff(); gain=delta.clip(lower=0).rolling(14).mean(); loss=(-delta.clip(upper=0)).rolling(14).mean()
    rsi=100-100/(1+(gain/(loss.replace(0,np.nan))))
    signals=(close>ma20)&(ma5>ma20)&(vol>vavg*1.15)&(rsi.fillna(50)<78)
    rets=[]
    inds=np.where(signals.to_numpy())[0]
    last_pick=-99
    for i in inds:
        if i<25 or i+5>=len(d) or i-last_pick<5: continue
        r=float((close.iloc[i+5]/close.iloc[i]-1)*100)
        rets.append(r); last_pick=i
    if not rets:
        return {'trades':0,'success_rate':None,'avg_return_5d':None,'label':'유사 신호 없음','note':'유사 기술신호 5거래일 후 수익률'}
    sr=sum(1 for r in rets if r>0)/len(rets)*100
    avg=float(np.mean(rets))
    label='🟢 과거 유사신호 양호' if len(rets)>=4 and sr>=60 and avg>0 else ('🟡 과거 유사신호 혼조' if sr>=45 else '🔴 과거 유사신호 약함')
    return {'trades':len(rets),'success_rate':round(sr,1),'avg_return_5d':round(avg,2),'label':label,
            'note':'전체 V70.7 판정의 성과가 아니라 단순 유사 기술신호의 과거 5일 결과입니다.'}


def build_split_trade_plan(entry_krw, target1_krw, target2_krw, stop1_krw, qty, tech):
    q=max(int(qty or 0),0)
    if q<=0:
        return {'buy_tranches':[],'sell_tranches':[],'summary':'현재 신규진입 수량 0주'}
    if tech in ('눌림목매매','지지라인매매','낙주매매'):
        b1=max(1,int(q*0.5)); b2=max(0,q-b1)
        buys=[{'label':'1차','qty':b1,'price_krw':int(entry_krw)}, {'label':'2차','qty':b2,'price_krw':int(max(stop1_krw,entry_krw*0.98))}] if b2 else [{'label':'1차','qty':b1,'price_krw':int(entry_krw)}]
    else:
        b1=max(1,int(q*0.4)); b2=max(0,q-b1)
        buys=[{'label':'1차','qty':b1,'price_krw':int(entry_krw)}, {'label':'확인매수','qty':b2,'price_krw':int(entry_krw*1.01)}] if b2 else [{'label':'1차','qty':b1,'price_krw':int(entry_krw)}]
    s1=max(1,int(q*0.5)); s2=max(0,int(q*0.3)); s3=max(0,q-s1-s2)
    sells=[{'label':'1차 익절','qty':s1,'price_krw':int(target1_krw)}]
    if s2: sells.append({'label':'2차 익절','qty':s2,'price_krw':int(target2_krw)})
    if s3: sells.append({'label':'추세보유','qty':s3,'price_krw':None})
    return {'buy_tranches':buys,'sell_tranches':sells,'summary':'분할매수 후 1차 50%·2차 30% 익절, 잔여는 추세보유'}

def build_price_outlook(df, close, ma, rsi, rvol, momentum, macd_bull, bb_break):
    """Rule-based 1~3 month outlook from price/volume. Not a price target forecast."""
    score = 50.0
    points = []
    ma20, ma60, ma120 = ma.get('ma20'), ma.get('ma60'), ma.get('ma120')
    r20 = calc_return(df, 20) if len(df) >= 21 else 0.0
    r60 = calc_return(df, 60) if len(df) >= 61 else r20
    if ma20 and close > ma20:
        score += 8; points.append('현재가가 20일선 위')
    else:
        score -= 8; points.append('현재가가 20일선 아래')
    if ma20 and ma60 and ma20 > ma60:
        score += 10; points.append('중기 이동평균 상승 구조')
    elif ma20 and ma60:
        score -= 8; points.append('중기 이동평균 약세 구조')
    if ma60 and ma120 and ma60 > ma120:
        score += 8; points.append('장기 추세 우상향')
    if r20 > 5:
        score += 6; points.append(f'20거래일 수익률 +{r20:.1f}%')
    elif r20 < -8:
        score -= 8; points.append(f'20거래일 수익률 {r20:.1f}%')
    if r60 > 10:
        score += 6
    elif r60 < -15:
        score -= 8
    if macd_bull:
        score += 5; points.append('MACD 상승 우위')
    else:
        score -= 3
    if rvol >= 1.5:
        score += 5; points.append(f'상대거래량 {rvol:.1f}배')
    if rsi is not None and rsi >= 78:
        score -= 8; points.append(f'RSI {rsi:.0f} 과열 주의')
    elif rsi is not None and 45 <= rsi <= 68:
        score += 3
    score += (float(momentum) - 50.0) * 0.15
    score = max(0.0, min(100.0, score))
    if score >= 72:
        label = '🟢 1~3개월 긍정'
        summary = '중기 추세와 수급이 비교적 양호합니다. 급등 추격보다 눌림에서 분할 접근하는 쪽이 낫습니다.'
    elif score >= 55:
        label = '🟡 중립·관찰'
        summary = '상승 여지는 있지만 신호가 완전히 정렬되지 않았습니다. 지지 확인 후 접근하는 편이 안전합니다.'
    else:
        label = '🔴 1~3개월 주의'
        summary = '추세·모멘텀 중 약한 항목이 많습니다. 신규매수보다 회복 신호를 기다리는 편이 낫습니다.'
    return round(score, 1), label, summary, points[:5]


def build_long_term_plan(df, close, ma, momentum, market_state, price_krw, settings):
    """Rule-based long-term suitability and 3-step accumulation plan."""
    score = 50.0
    reasons = []
    ma20, ma60, ma120 = ma.get('ma20'), ma.get('ma60'), ma.get('ma120')
    r60 = calc_return(df, 60) if len(df) >= 61 else calc_return(df, 20)
    if ma60 and close > ma60:
        score += 12; reasons.append('60일선 위에서 거래')
    else:
        score -= 8; reasons.append('60일선 아래')
    if ma60 and ma120 and ma60 > ma120:
        score += 14; reasons.append('60일선 > 120일선')
    elif ma120:
        score -= 8
    if r60 > 8:
        score += 8; reasons.append(f'최근 60거래일 +{r60:.1f}%')
    elif r60 < -15:
        score -= 10; reasons.append(f'최근 60거래일 {r60:.1f}%')
    score += (float(momentum) - 50.0) * 0.12
    if '급락' in market_state:
        score -= 8
    score = max(0.0, min(100.0, score))
    if score >= 72:
        action = '🟢 장기 분할매수 후보'
    elif score >= 55:
        action = '🟡 장기 관찰·소액분할'
    else:
        action = '🔴 장기 신규매수 보류'
    long_budget = max(int(settings.get('long_budget', 1_000_000)), 0)
    total_qty = int(long_budget // price_krw) if price_krw > 0 and score >= 55 else 0
    q1 = int(total_qty * 0.4)
    q2 = int(total_qty * 0.3)
    q3 = max(0, total_qty - q1 - q2)
    # Prefer technical supports, but never invent precision beyond rule-based levels.
    p1 = price_krw
    p2_native = ma20 if ma20 and ma20 < close else close * 0.95
    p3_native = ma60 if ma60 and ma60 < close else close * 0.90
    mult = price_krw / close if close else 1.0
    return {
        'score': round(score, 1), 'action': action, 'reasons': reasons[:4],
        'budget_krw': long_budget, 'qty': total_qty,
        'tranches': [
            {'label':'1차','price_krw':_safe_int(p1),'qty':q1},
            {'label':'2차','price_krw':_safe_int(p2_native*mult),'qty':q2},
            {'label':'3차','price_krw':_safe_int(p3_native*mult),'qty':q3},
        ]
    }



def build_safety_and_quality(df, close, ma, atr_pct, rsi, recent5, trading_value_krw, long_plan, market, price_krw, total_budget, trade_budget):
    """Beginner safety gates and a conservative long-term quality candidate label.
    This is not a fundamental-quality certification; fundamentals can confirm it later.
    """
    flags=[]
    hard_block=False
    # Data freshness: allow weekends/holidays, block clearly stale daily data.
    stale_days=None
    try:
        last=pd.Timestamp(df.index[-1]).tz_localize(None)
        now=pd.Timestamp.now().tz_localize(None)
        stale_days=max(0, int((now.normalize()-last.normalize()).days))
        if stale_days>=7:
            flags.append(f'가격 데이터 {stale_days}일 경과')
            hard_block=True
    except Exception:
        pass
    if recent5>=15 or (rsi is not None and rsi>=82):
        flags.append('단기 급등·과열: 추격매수 주의')
    if atr_pct>=9:
        flags.append(f'변동성 매우 높음(ATR {atr_pct:.1f}%)')
    # Liquidity floor is intentionally low enough not to exclude ordinary liquid names.
    liq_floor=500_000_000 if market!='US' else 1_000_000_000
    if trading_value_krw<liq_floor:
        flags.append('거래대금이 적어 체결·급변 위험')
        hard_block=True
    if total_budget>0 and trade_budget>total_budget*0.25:
        flags.append('한 종목 예산이 전체 자금의 25% 초과')
    ma60,ma120=ma.get('ma60'),ma.get('ma120')
    long_score=float((long_plan or {}).get('score',0))
    quality=(long_score>=75 and ma60 and ma120 and close>ma60>ma120 and atr_pct<=7.0 and trading_value_krw>=liq_floor and recent5<15)
    if quality:
        qlabel='💎 장기 우량 ETF 후보' if market=='ETF' else '💎 장기 우량주 후보'
        qreason='중장기 추세·유동성·변동성 기준 통과 · 실적 확인 전'
    else:
        qlabel=None; qreason=None
    return {
        'risk_flags':flags[:5], 'hard_block':hard_block, 'stale_days':stale_days,
        'long_quality_candidate':bool(quality), 'long_quality_label':qlabel, 'long_quality_reason':qreason
    }

def build_held_action(held_price, held_qty, close_krw, momentum, rsi, grade, ma20_krw, stop1_krw, qty_add, rvol=1.0, flow_score=50.0):
    if held_price <= 0:
        return None
    pnl_pct = (close_krw / held_price - 1) * 100
    pnl_krw = (close_krw - held_price) * max(held_qty, 0)
    trend_broken = (ma20_krw > 0 and close_krw < ma20_krw) or momentum < 40 or close_krw <= stop1_krw
    recovery = []
    add_allowed = False
    if pnl_pct <= -12:
        action='🚨 긴급 손실관리'
        if trend_broken:
            reason='손실 폭이 크고 추세까지 약합니다. 추가매수보다 현금 회수가 우선입니다.'
            recovery=['추가매수 금지','반등 시 30~50% 비중축소 검토','손절선 재이탈 시 잔여 물량 정리 검토']
        else:
            reason='큰 손실 구간이지만 추세가 완전히 무너지진 않았습니다. 한 번에 복구하려 하지 말고 비중부터 관리합니다.'
            recovery=['추가매수는 원칙적으로 보류','20일선 회복 유지 여부 확인','반등 시 일부 비중축소로 리스크 감소']
    elif pnl_pct <= -7:
        action='🔴 손실 축소 우선'
        reason='중간 이상 손실 구간입니다. 물타기보다 추세 확인과 비중 조절이 우선입니다.'
        recovery=['추세 붕괴 시 30~50% 분할손절 검토','20일선 회복 전 공격적 물타기 금지','거래량 동반 반등 시 탈출/축소 가격 재설정']
    elif pnl_pct <= -3:
        action='🟠 경계·반등 확인'
        if not trend_broken and grade in ('S','A') and flow_score>=55 and qty_add>0:
            reason='손실은 있지만 추세·수급 프록시가 아직 버티고 있습니다. 허용손실 안에서만 소액 대응합니다.'
            recovery=['기존 손절선 유지','추가매수는 1회·소액만 검토','반등 실패 시 즉시 추가매수 중단']
            add_allowed=True
        else:
            reason='손실이 커지기 시작한 구간입니다. 추가매수보다 손절선과 20일선을 우선 확인합니다.'
            recovery=['추가매수 대기','20일선 이탈 지속 시 비중축소','본전 집착보다 시나리오 무효화 여부 확인']
    elif pnl_pct < 0:
        action='🟡 정상 손실 범위·관찰'
        reason='작은 손실 구간입니다. 계획한 손절선 안이면 성급한 대응보다 원래 시나리오를 확인합니다.'
        recovery=['손절선 임의 확대 금지','추세 유지 시 관찰','거래량 급증 하락이면 조기 비중축소 검토']
    elif pnl_pct >= 12 and (rsi is not None and rsi >= 75):
        action='🟠 일부매도·익절'
        reason='수익 구간이면서 단기 과열 신호가 있어 일부 이익실현이 유리한 구간입니다.'
        recovery=['최소 30~50% 이익실현 검토','잔여 물량은 추세손절로 보호']
    elif grade in ('S','A') and momentum >= 70 and close_krw >= ma20_krw:
        action='🟢 보유 우세'
        reason='추세와 모멘텀이 아직 양호합니다. 목표가·손절 기준을 올려가며 보유를 검토합니다.'
        recovery=['1차 목표 도달 시 일부익절','20일선 이탈 시 재평가']
    else:
        action='🟡 보유·관찰'
        reason='즉시 매수나 매도 신호가 강하지 않습니다. 20일선과 손절가를 기준으로 대응합니다.'
        recovery=['추세 확인 후 대응','손절선 아래에서는 물타기 금지']
    return {
        'action': action, 'reason': reason, 'pnl_pct': round(pnl_pct,2),
        'pnl_krw': _safe_int(pnl_krw), 'held_qty': int(max(held_qty,0)),
        'market_value_krw': _safe_int(close_krw*max(held_qty,0)),
        'add_qty': int(max(qty_add,0)) if add_allowed else 0,
        'loss_stage': ('긴급' if pnl_pct<=-12 else '위험' if pnl_pct<=-7 else '경계' if pnl_pct<=-3 else '정상'),
        'recovery_steps': recovery[:4], 'trend_broken': bool(trend_broken),
        'avg_recovery_pct': round((held_price/close_krw-1)*100,2) if close_krw>0 and pnl_pct<0 else 0.0,
    }


def build_fundamental_outlook(out):
    score = 50.0
    reasons = []
    rg, eg, om = out.get('revenue_growth'), out.get('earnings_growth'), out.get('operating_margin')
    pe = out.get('trailing_pe')
    if rg is not None:
        if rg > 0.10: score += 12; reasons.append(f'매출 성장률 {rg*100:.1f}%')
        elif rg < 0: score -= 10; reasons.append(f'매출 역성장 {rg*100:.1f}%')
    if eg is not None:
        if eg > 0.10: score += 12; reasons.append(f'이익 성장률 {eg*100:.1f}%')
        elif eg < 0: score -= 12; reasons.append(f'이익 감소 {eg*100:.1f}%')
    if om is not None:
        if om > 0.15: score += 10; reasons.append(f'영업이익률 {om*100:.1f}%')
        elif om < 0: score -= 12; reasons.append('영업적자 구간')
    if pe is not None:
        if 0 < pe < 35: score += 5; reasons.append(f'PER {pe:.1f}')
        elif pe > 80: score -= 5; reasons.append(f'고PER {pe:.1f}')
    score = max(0.0, min(100.0, score))
    if score >= 70: label='🟢 실적 기반 장기 긍정'
    elif score >= 52: label='🟡 실적 중립·확인 필요'
    else: label='🔴 실적 기반 장기 주의'
    return round(score,1), label, reasons[:4]

def fetch_fundamentals(code: str, market: str):
    """Best-effort fundamentals. Returns nulls instead of inventing values."""
    last_error = None
    for symbol in _yahoo_symbol_candidates(code, market):
        try:
            t = yf.Ticker(symbol)
            info = t.info or {}
            if not info and market != 'US':
                continue
            def num(k):
                v = info.get(k)
                return None if v is None else _safe_float(v, None)
            out = {
                'symbol': symbol,
                'revenue': num('totalRevenue'),
                'revenue_growth': num('revenueGrowth'),
                'net_income': num('netIncomeToCommon'),
                'earnings_growth': num('earningsGrowth'),
                'operating_margin': num('operatingMargins'),
                'profit_margin': num('profitMargins'),
                'trailing_pe': num('trailingPE'),
                'forward_pe': num('forwardPE'),
                'market_cap': num('marketCap'),
                'fifty_two_week_change': num('52WeekChange'),
                'currency': info.get('currency'),
                'earnings_timestamp': info.get('earningsTimestamp') or info.get('earningsTimestampStart'),
                'source': 'Yahoo Finance',
            }
            # If quoteSummary gives almost no useful fields, try quarterly income statement.
            if out['revenue'] is None and out['net_income'] is None:
                try:
                    qf = t.quarterly_financials
                    if qf is not None and not qf.empty:
                        cols = list(qf.columns)
                        if cols:
                            c0 = cols[0]
                            for label in ('Total Revenue', 'TotalRevenue'):
                                if label in qf.index:
                                    out['revenue'] = _safe_float(qf.loc[label, c0], None)
                                    break
                            for label in ('Net Income', 'NetIncome'):
                                if label in qf.index:
                                    out['net_income'] = _safe_float(qf.loc[label, c0], None)
                                    break
                except Exception:
                    pass
            if any(out.get(k) is not None for k in ('revenue','net_income','trailing_pe','market_cap','operating_margin')):
                fs, fl, fr = build_fundamental_outlook(out)
                out['outlook_score'] = fs
                out['outlook_label'] = fl
                out['outlook_reasons'] = fr
                out['quality_confirmed'] = bool(fs >= 70 and (out.get('operating_margin') is None or out.get('operating_margin') > 0) and (out.get('earnings_growth') is None or out.get('earnings_growth') >= 0))
                out['quality_label'] = '💎 실적확인 우량' if out['quality_confirmed'] else '실적 확인상 우량 확정 아님'
                ts=out.get('earnings_timestamp')
                out['earnings_date']=None; out['earnings_risk']=None
                if ts:
                    try:
                        dt=pd.to_datetime(float(ts), unit='s', utc=True)
                        days=(dt-pd.Timestamp.now(tz='UTC')).total_seconds()/86400
                        out['earnings_date']=dt.isoformat()
                        if 0 <= days <= 7: out['earnings_risk']=f'⚠️ 실적발표 약 {max(0,int(round(days)))}일 전후 — 신규진입 변동성 주의'
                        elif -1 <= days < 0: out['earnings_risk']='⚠️ 실적발표 당일/직후 가능 — 갭 변동 주의'
                    except Exception:
                        pass
                return out
        except Exception as e:
            last_error = str(e)
            continue
    return {
        'symbol': code, 'revenue': None, 'revenue_growth': None, 'net_income': None,
        'earnings_growth': None, 'operating_margin': None, 'profit_margin': None,
        'trailing_pe': None, 'forward_pe': None, 'market_cap': None,
        'fifty_two_week_change': None, 'currency': None, 'source': 'Yahoo Finance',
        'outlook_score': None, 'outlook_label': '실적 데이터 부족', 'outlook_reasons': [], 'quality_confirmed': False, 'quality_label':'실적 데이터 부족', 'earnings_timestamp':None, 'earnings_date':None, 'earnings_risk':None,
        'error': ('실적 데이터 수신 실패' + (f': {last_error[:100]}' if last_error else ''))
    }


def diagnostic_market():
    result = {'kospi_chg': None, 'kosdaq_chg': None, 'kospi_5d': None, 'kosdaq_5d': None, 'sp500_5d': None, 'state': '데이터 확인 중', 'guide': '지수 데이터 확인이 필요합니다.'}
    try:
        kp = fdr.DataReader('KS11').tail(10)
        kq = fdr.DataReader('KQ11').tail(10)
        kpchg = (float(kp['Close'].iloc[-1]) / float(kp['Close'].iloc[-2]) - 1) * 100
        kqchg = (float(kq['Close'].iloc[-1]) / float(kq['Close'].iloc[-2]) - 1) * 100
        result['kospi_chg'], result['kosdaq_chg'] = kpchg, kqchg
        if len(kp)>=6: result['kospi_5d']=(float(kp['Close'].iloc[-1])/float(kp['Close'].iloc[-6])-1)*100
        if len(kq)>=6: result['kosdaq_5d']=(float(kq['Close'].iloc[-1])/float(kq['Close'].iloc[-6])-1)*100
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
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sp=yf.download('SPY',period='10d',progress=False,auto_adjust=False,threads=False)
        if isinstance(sp.columns,pd.MultiIndex): sp.columns=sp.columns.droplevel(1)
        if sp is not None and len(sp)>=6: result['sp500_5d']=(float(sp['Close'].dropna().iloc[-1])/float(sp['Close'].dropna().iloc[-6])-1)*100
    except Exception:
        pass
    return result


def _load_price(code, market):
    if market == 'US':
        # US search must be resilient on cloud hosts. Yahoo can occasionally
        # return an empty frame / rate-limit Render IPs, so try multiple
        # canonical ticker forms and then FinanceDataReader as a second source.
        raw = str(code or '').strip().upper()
        yf_codes = []
        for cand in (raw, raw.replace('.', '-'), raw.replace('/', '-')):
            if cand and cand not in yf_codes:
                yf_codes.append(cand)

        for ticker in yf_codes:
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    df = yf.download(ticker, period='6mo', interval='1d', progress=False, auto_adjust=False, threads=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                if df is not None and not df.empty and 'Close' in df.columns and df['Close'].dropna().shape[0] >= 20:
                    return df
            except Exception:
                pass

        # FDR can provide many US tickers through its alternate data source.
        for ticker in (raw, raw.replace('-', '.'), raw.replace('.', '-')):
            if not ticker:
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    df = fdr.DataReader(ticker)
                if df is not None and not df.empty:
                    df = df.tail(180)
                    # Normalize common FDR column naming if needed.
                    rename = {}
                    for c in df.columns:
                        lc = str(c).lower()
                        if lc == 'close': rename[c] = 'Close'
                        elif lc == 'open': rename[c] = 'Open'
                        elif lc == 'high': rename[c] = 'High'
                        elif lc == 'low': rename[c] = 'Low'
                        elif lc == 'volume': rename[c] = 'Volume'
                    if rename:
                        df = df.rename(columns=rename)
                    if 'Close' in df.columns and df['Close'].dropna().shape[0] >= 20:
                        return df
            except Exception:
                pass
        return pd.DataFrame()
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
        current_day_chg = (close / prev_close - 1) * 100 if prev_close else 0
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
        avg_vol20 = _safe_float(df['Volume'].tail(20).mean(), 0) if 'Volume' in df.columns else 0
        trading_value_krw = close * vol * krw_mult
        timing = get_trade_timing(tech, market)
        taxonomy = classify_theme_detail(info.get('name', code), info.get('theme', ''), info.get('source_sector',''))
        sector = taxonomy['industry']
        benchmark5 = _safe_float(settings.get('_sp500_5d') if market == 'US' else settings.get('_kospi_5d'), 0.0)
        relative_strength_5d = recent5 - benchmark5
        above_ma20 = bool(ma.get('ma20') and close > ma.get('ma20'))
        attention_score, attention_label, attention_reasons = build_attention_signal(
            close, high20, rvol, trading_value_krw, recent5, bb_break, info.get('theme', '')
        )
        flow_proxy = build_flow_proxy(df)
        backtest = build_backtest_proxy(df)

        decision = {
            'S': '최우선 후보', 'A': '강한 후보', 'B': '분할 검토', 'C': '관망'
        }[grade]

        held_price = _safe_float(info.get('my_price'), 0)
        held_qty = int(max(_safe_float(info.get('held_qty'), 0), 0))
        held_pnl = None
        if held_price > 0:
            held_pnl = (close_krw / held_price - 1) * 100

        outlook_score, outlook_label, outlook_summary, outlook_points = build_price_outlook(
            df, close, ma, rsi, rvol, momentum, macd_bull, bb_break
        )
        long_plan = build_long_term_plan(df, close, ma, momentum, market_state, close_krw, settings)
        safety = build_safety_and_quality(
            df, close, ma, atr_pct, rsi, recent5, trading_value_krw, long_plan, market,
            close_krw, total_budget, trade_budget
        )
        # Beginner safety: do not recommend a new position when a hard safety gate fails.
        if safety['hard_block'] and held_price <= 0:
            qty = 0
            invested = expected_profit = expected_loss = 0
            if grade != 'C':
                decision = '안전장치로 신규진입 보류'
        held_action = build_held_action(
            held_price, held_qty, close_krw, momentum, rsi, grade,
            _safe_int(ma.get('ma20', 0) * krw_mult), _safe_int(stop1 * krw_mult), qty,
            rvol=rvol, flow_score=flow_proxy.get('score',50)
        )
        split_plan = build_split_trade_plan(_safe_int(entry_krw), _safe_int(target1*krw_mult), _safe_int(target2*krw_mult), _safe_int(stop1*krw_mult), qty, tech)

        return {
            'ok': True,
            'code': code,
            'name': info.get('name', code),
            'market': market,
            'category': 'US' if market == 'US' else ('ETF' if market == 'ETF' else 'KR'),
            'theme': info.get('theme', ''),
            'sector': sector,
            'sector_major': taxonomy['major'],
            'theme_tags': taxonomy['theme_tags'],
            'relative_strength_5d': round(relative_strength_5d, 2),
            'above_ma20': above_ma20,
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
            'current_day_pct': round(current_day_chg, 2),
            'prev_day_pct': round(prev_day_chg, 2),
            'prev_close_krw': _safe_int(prev_close * krw_mult),
            'recent5_pct': round(recent5, 2),
            'ma5_krw': _safe_int(ma.get('ma5', 0) * krw_mult),
            'ma20_krw': _safe_int(ma.get('ma20', 0) * krw_mult),
            'vwap_proxy_krw': _safe_int(vwap_proxy * krw_mult),
            'pivot_s1_krw': _safe_int(s1 * krw_mult),
            'pivot_pp_krw': _safe_int(pp * krw_mult),
            'pivot_r1_krw': _safe_int(r1 * krw_mult),
            'macd_bull': macd_bull,
            'bb_break': bb_break,
            'volume': _safe_int(vol),
            'avg_volume20': _safe_int(avg_vol20),
            'trading_value_krw': _safe_int(trading_value_krw),
            'attention_score': attention_score,
            'attention_label': attention_label,
            'attention_reasons': attention_reasons,
            'flow_proxy': flow_proxy,
            'backtest': backtest,
            'split_plan': split_plan,
            'buy_time': timing['buy_time'],
            'buy_rule': timing['buy_rule'],
            'sell_plan': timing['sell_plan'],
            'time_stop': timing['time_stop'],
            'time_zone_note': timing['time_zone_note'],
            'tech': tech,
            'strategy_reason': reason,
            'strategy_confidence': round(confidence * 100, 0),
            'held_price_krw': _safe_int(held_price) if held_price > 0 else None,
            'held_qty': held_qty if held_price > 0 else None,
            'held_pnl_pct': None if held_pnl is None else round(held_pnl, 2),
            'held_action': held_action,
            'outlook_score': outlook_score,
            'outlook_label': outlook_label,
            'outlook_summary': outlook_summary,
            'outlook_points': outlook_points,
            'long_term': long_plan,
            **safety,
            'after_hours': None,
            'market_regime': market_state,
            'data_note': '일봉 기반 기술지표. 시간외 가격은 제공하지 않음. 수급은 가격·거래량 프록시이며 실제 외국인/기관 순매수와 다릅니다.',
        }
    except Exception as e:
        return {
            'ok': False, 'code': code, 'name': info.get('name', code), 'market': market,
            'category': 'US' if market == 'US' else ('ETF' if market == 'ETF' else 'KR'),
            'grade': 'C', 'decision': '데이터 오류', 'error': str(e)[:120], 'theme': info.get('theme', '')
        }


def parse_held(text: str):
    """Format: 삼성전자:75000:20, 005930:75000:20, NVDA:120:5"""
    result = []
    if not text:
        return result
    mapping = get_krx_mapping()
    reverse = {c:n for n,c in mapping.items()}
    for raw in text.split(','):
        item = raw.strip()
        if not item:
            continue
        parts = [x.strip() for x in item.split(':')]
        key = parts[0] if parts else ''
        price = _safe_float(parts[1].replace(',', '').replace('원', ''), 0) if len(parts) > 1 else 0
        qty = int(max(_safe_float(parts[2].replace('주',''), 0), 0)) if len(parts) > 2 else 0
        if not key:
            continue
        # Alphabetic ticker => US holding.
        if any(ch.isalpha() for ch in key) and key not in mapping:
            code = key.upper().replace(' ', '')
            info = dict(US_CURATED.get(code) or {'name': code, 'theme': '보유/검색 종목', 'target_pct': 4.0, 'prob': 60, 'tech': '추세매매'})
            info.update({'theme':'보유/검색 종목', 'my_price': price, 'held_qty': qty})
            result.append((code, info, 'US'))
            continue
        code, name = '', ''
        if key.isdigit():
            code = key.zfill(6); name = reverse.get(code, code)
        elif key in mapping:
            code, name = mapping[key], key
        if code:
            info = dict(KR_CURATED.get(code) or KR_ETFS.get(code) or {'name':name, 'theme':'보유/검색 종목', 'target_pct':4.0, 'prob':60, 'tech':'추세매매'})
            info.update({'name': info.get('name') or name, 'theme':'보유/검색 종목', 'my_price':price, 'held_qty':qty})
            market = 'ETF' if code in KR_ETFS else 'KR'
            result.append((code, info, market))
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
    settings = dict(settings)
    settings['_kospi_5d'] = market.get('kospi_5d') or 0.0
    settings['_sp500_5d'] = market.get('sp500_5d') or 0.0
    mode = settings.get('mode', 'curated')
    top_n = max(10, min(int(settings.get('top_n', 60)), 150))
    kr = build_universe(mode, top_n)
    us = dict(US_CURATED)
    etf = dict(KR_ETFS)
    held = parse_held(settings.get('held', ''))

    tasks: List[Tuple[str, Dict[str, Any], str]] = []
    # Held first so they always appear.
    for c, i, hm in held:
        tasks.append((c, i, hm))
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
    sector_analysis = build_sector_analytics(valid)
    summary = {
        'total': len(results),
        'valid': len(valid),
        'recommended': len(recommended),
        's_count': sum(1 for x in valid if x.get('grade') == 'S'),
        'a_count': sum(1 for x in valid if x.get('grade') == 'A'),
        'long_quality_count': sum(1 for x in valid if x.get('long_quality_candidate')),
        'elapsed_sec': round(time.time() - started, 1),
        'usdkrw': round(fx, 2),
    }
    return {'market': market, 'summary': summary, 'sector_analysis': sector_analysis, 'results': results}

# ---------------------------------------------------------------------
# V70.4 on-demand symbol search
# ---------------------------------------------------------------------
def _resolve_search_query(query: str, market_hint: str = 'AUTO'):
    q = str(query or '').strip()
    if not q:
        raise ValueError('종목명 또는 종목코드를 입력하세요.')
    hint = str(market_hint or 'AUTO').upper()
    qu = q.upper()

    # Common US company-name aliases for beginner search.
    us_aliases = {
        '엔비디아':'NVDA', 'NVIDIA':'NVDA',
        '애플':'AAPL', 'APPLE':'AAPL',
        '테슬라':'TSLA', 'TESLA':'TSLA',
        '마이크로소프트':'MSFT', 'MICROSOFT':'MSFT',
        '팔란티어':'PLTR', 'PALANTIR':'PLTR',
        '아마존':'AMZN', 'AMAZON':'AMZN',
        '알파벳':'GOOGL', '구글':'GOOGL', 'GOOGLE':'GOOGL', 'ALPHABET':'GOOGL',
        '메타':'META', 'META PLATFORMS':'META',
        'AMD':'AMD', '브로드컴':'AVGO', 'BROADCOM':'AVGO',
        '넷플릭스':'NFLX', 'NETFLIX':'NFLX',
        '코스트코':'COST', 'COSTCO':'COST',
        '월마트':'WMT', 'WALMART':'WMT',
        'JP모건':'JPM', 'JPMORGAN':'JPM',
    }
    alias_code = us_aliases.get(q) or us_aliases.get(qu)
    if alias_code:
        info = dict(US_CURATED.get(alias_code) or {'name': q, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'})
        return alias_code, info, 'US'

    # Known curated Korean/US names first.
    for code, info in {**KR_CURATED, **KR_ETFS}.items():
        if q == info.get('name') or qu == code.upper():
            market = 'ETF' if code in KR_ETFS else 'KR'
            return code, dict(info), market
    for code, info in US_CURATED.items():
        if q.lower() == str(info.get('name', '')).lower() or qu == code.upper():
            return code, dict(info), 'US'

    if hint == 'US':
        code = qu.replace(' ', '')
        return code, {'name': code, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'}, 'US'

    mapping = get_krx_mapping()
    if q.isdigit():
        code = q.zfill(6)
        name = next((n for n, c in mapping.items() if c == code), code)
        market = 'ETF' if code in KR_ETFS else 'KR'
        info = dict(KR_ETFS.get(code) or KR_CURATED.get(code) or {'name': name, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'})
        info['name'] = info.get('name') or name
        return code, info, market

    if q in mapping:
        code = mapping[q]
        market = 'ETF' if code in KR_ETFS else 'KR'
        info = dict(KR_ETFS.get(code) or KR_CURATED.get(code) or {'name': q, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'})
        return code, info, market

    # Korean partial-name match: prefer startswith, then shortest matching name.
    matches = [(name, code) for name, code in mapping.items() if q.lower() in name.lower()]
    if matches and hint != 'US':
        matches.sort(key=lambda x: (0 if x[0].lower().startswith(q.lower()) else 1, len(x[0]), x[0]))
        name, code = matches[0]
        market = 'ETF' if code in KR_ETFS else 'KR'
        info = dict(KR_ETFS.get(code) or KR_CURATED.get(code) or {'name': name, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'})
        return code, info, market

    # AUTO fallback: alphabetic ticker -> US.
    if all(ch.isalnum() or ch in '.-' for ch in qu) and any(ch.isalpha() for ch in qu):
        code = qu.replace(' ', '')
        return code, {'name': code, 'theme': '직접 검색 종목', 'target_pct': 4.0, 'prob': 70, 'tech': '추세매매'}, 'US'

    raise ValueError(f'종목을 찾지 못했습니다: {q}')


def analyze_search(query: str, settings: Dict[str, Any], market_hint: str = 'AUTO'):
    started = time.time()
    code, info, market = _resolve_search_query(query, market_hint)
    fx = get_usdkrw_rate()
    market_info = diagnostic_market()
    item = analyze_one(code, info, market, settings, fx, market_info['state'])
    if not item.get('ok'):
        raise ValueError(item.get('error') or '종목 데이터 분석에 실패했습니다.')
    return {
        'query': query,
        'resolved': {'code': code, 'name': item.get('name'), 'market': market},
        'market': market_info,
        'usdkrw': round(fx, 2),
        'item': item,
        'elapsed_sec': round(time.time() - started, 1),
    }
