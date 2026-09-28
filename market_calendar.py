"""V78.12.1 shared market calendar (KRX / US regular sessions).

Single source of truth for holidays, early closes and session phases so the server,
the analysis engine and the browser (via /health) never disagree about whether a
market is open.  Extra dates can be injected without a redeploy of code via
EXTRA_KR_HOLIDAYS / EXTRA_US_HOLIDAYS environment variables (comma separated
YYYY-MM-DD), which is useful when a temporary holiday (e.g. election day) is announced.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
ET = ZoneInfo('America/New_York')

# KRX closed days (weekdays only; weekends are handled separately).
# 2026: 근로자의날(5/1)·지방선거(6/3)·제헌절 공휴일 재지정(7/17)·연말휴장(12/31) 포함.
_KR_BASE = {
    # 2026
    '2026-01-01', '2026-02-16', '2026-02-17', '2026-02-18', '2026-03-02',
    '2026-05-01', '2026-05-05', '2026-05-25', '2026-06-03', '2026-07-17',
    '2026-08-17', '2026-09-24', '2026-09-25', '2026-10-05', '2026-10-09',
    '2026-12-25', '2026-12-31',
    # 2027 (설날 대체 2/9, 광복절 대체 8/16, 개천절 대체 10/4, 한글날 대체 10/11,
    #       성탄절 대체 12/27, 제헌절 7/17(토) 대체 7/19, 연말휴장 12/31)
    '2027-01-01', '2027-02-08', '2027-02-09', '2027-03-01', '2027-05-05',
    '2027-05-13', '2027-07-19', '2027-08-16', '2027-09-14', '2027-09-15',
    '2027-09-16', '2027-10-04', '2027-10-11', '2027-12-27', '2027-12-31',
}
# NYSE/Nasdaq full-day closures.
_US_BASE = {
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
    '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
    '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24',
}
# NYSE/Nasdaq 13:00 ET early closes.
US_EARLY_CLOSE = {'2026-11-27', '2026-12-24', '2027-11-26'}
CALENDAR_COVERED_YEARS = (2026, 2027)


def _env_dates(name):
    raw = os.environ.get(name, '')
    return {x.strip() for x in raw.split(',') if len(x.strip()) == 10}


KR_HOLIDAYS = frozenset(_KR_BASE | _env_dates('EXTRA_KR_HOLIDAYS'))
US_HOLIDAYS = frozenset(_US_BASE | _env_dates('EXTRA_US_HOLIDAYS'))


def normalize_market(market):
    return 'US' if str(market or '').upper() == 'US' else 'KR'


def is_holiday(market, date_key):
    return date_key in (US_HOLIDAYS if normalize_market(market) == 'US' else KR_HOLIDAYS)


def regular_hours(market, date_key=None):
    """(open_minute, close_minute) of the regular session in local exchange time."""
    if normalize_market(market) == 'US':
        close = 13 * 60 if date_key in US_EARLY_CLOSE else 16 * 60
        return 9 * 60 + 30, close
    return 9 * 60, 15 * 60 + 30


def market_session(market, now=None):
    """Market phase. `open` means *regular session open* only, so an extended-hours
    observation is never upgraded into an immediate-buy signal."""
    market = normalize_market(market)
    tz = ET if market == 'US' else KST
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    date_key = now.strftime('%Y-%m-%d')
    weekend = now.weekday() >= 5
    holiday = is_holiday(market, date_key)
    mins = now.hour * 60 + now.minute
    open_min, close_min = regular_hours(market, date_key)
    state, label, regular_open, extended_open = 'closed', '장 마감', False, False
    if weekend or holiday:
        label = '주말 휴장' if weekend else '공휴일 휴장'
    elif market == 'KR':
        # KRX: 동시호가 08:30~09:00, 정규 09:00~15:30, 시간외 15:40~18:00.
        if mins < 8 * 60 + 30:
            state, label = 'pre_wait', '개장 전'
        elif mins < open_min:
            state, label = 'preopen', '시가 동시호가'
        elif mins < close_min:
            state, label, regular_open = 'regular', '정규장 거래중', True
        elif mins < 15 * 60 + 40:
            state, label = 'post_wait', '정규장 종료·시간외 대기'
        elif mins < 18 * 60:
            state, label, extended_open = 'post', '시간외 거래', True
        else:
            state, label = 'after', '장 마감'
    else:
        # Nasdaq: pre 04:00~09:30, regular 09:30~16:00 (early close 13:00), after ~20:00 ET.
        after_end = 17 * 60 if date_key in US_EARLY_CLOSE else 20 * 60
        if mins < 4 * 60:
            state, label = 'pre_wait', '프리마켓 대기'
        elif mins < open_min:
            state, label, extended_open = 'premarket', '프리마켓', True
        elif mins < close_min:
            state, label, regular_open = 'regular', ('정규장 거래중(조기폐장일)' if date_key in US_EARLY_CLOSE else '정규장 거래중'), True
        elif mins < after_end:
            state, label, extended_open = 'afterhours', '애프터마켓', True
        else:
            state, label = 'after', '장 마감'
    return {
        'market': market, 'open': regular_open, 'regular_open': regular_open,
        'extended_open': extended_open, 'tradable': regular_open or extended_open,
        'state': state, 'label': label, 'date': date_key,
        'local_time': now.strftime('%H:%M'),
        'timezone': 'America/New_York' if market == 'US' else 'Asia/Seoul',
        'early_close': bool(market == 'US' and date_key in US_EARLY_CLOSE),
        'calendar_covered': now.year in CALENDAR_COVERED_YEARS,
        'policy': 'regular_session_only_for_immediate_signal',
    }


def market_sessions():
    return {'KR': market_session('KR'), 'US': market_session('US')}


def holiday_payload():
    return {'KR': sorted(KR_HOLIDAYS), 'US': sorted(US_HOLIDAYS),
            'US_EARLY_CLOSE': sorted(US_EARLY_CLOSE), 'covered_years': list(CALENDAR_COVERED_YEARS)}
