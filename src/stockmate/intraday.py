"""차트의 '오늘' 보기: 하루 분봉 (1분), 그날 최고가·최저가, 어제 종가.

- 국내 종목: 네이버 증권 분봉 (지연 없음, 장 마감 뒤 NXT 거래도 들어 있다). 분마다 종가와 누적 거래량만 준다.
- 미국 종목·시장 지표: yfinance 1분봉 (마지막 거래일, 미국 종목은 프리·애프터마켓 포함). 시간은 모두 한국 시간으로 바꾼다.
점마다 정규장 밖 거래인지(국내 "nxt", 미국 "pre"/"post")를 붙인다.
같은 종목을 여러 번 열어도 30초 동안은 다시 받지 않는다.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from stockmate.market_data import us_session_of
from stockmate.models import Market

log = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")
TTL = dt.timedelta(seconds=30)
_NAVER = "https://fchart.stock.naver.com/sise.nhn?symbol={symbol}&timeframe=minute&count=900&requestType=0"
_ITEM = re.compile(r'data="(\d{12})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|(\d+)"')

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[dt.datetime, dict[str, Any] | None]] = {}


def parse_naver(text: str) -> list[tuple[dt.datetime, float, float]]:
    """네이버 분봉 XML → [(시각, 종가, 그 분의 거래량)]. 가장 최근 날짜만 남긴다. 거래량은 누적값의 차이로 구한다."""
    rows = [(dt.datetime.strptime(t, "%Y%m%d%H%M"), float(c), float(v)) for t, c, v in _ITEM.findall(text)]
    if not rows:
        return []
    last_day = rows[-1][0].date()
    day = [r for r in rows if r[0].date() == last_day]
    out, prev_cum = [], 0.0
    for t, close, cum in day:
        out.append((t.replace(tzinfo=SEOUL), close, max(cum - prev_cum, 0.0)))
        prev_cum = cum
    return out


def _naver_minutes(symbol: str) -> list[tuple[dt.datetime, float, float]]:
    request = urllib.request.Request(_NAVER.format(symbol=symbol), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return parse_naver(response.read().decode("euc-kr", errors="replace"))


def _yf_minutes(ticker: str, prepost: bool = False) -> tuple[list[tuple[dt.datetime, float, float]], float | None]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    frame = t.history(period="1d", interval="1m", prepost=prepost)
    rows = [
        (idx.to_pydatetime().astimezone(SEOUL), float(r["Close"]), float(r.get("Volume", 0) or 0))
        for idx, r in frame.iterrows()
        if r["Close"] == r["Close"]  # NaN 제외
    ]
    try:
        prev = float(t.fast_info["previous_close"])
    except Exception:
        prev = None
    return rows, prev


KRX_OPEN, KRX_CLOSE = dt.time(9, 0), dt.time(15, 30)


def session_of(market: str, moment: dt.datetime) -> str:
    """정규장 밖 거래 표시: 국내 "nxt", 미국 "pre"/"post", 정규장·지표는 ""."""
    if market == Market.KRX.value:
        t = moment.astimezone(SEOUL).time()
        return "" if KRX_OPEN <= t <= KRX_CLOSE else "nxt"
    if market == Market.US.value:
        return us_session_of(moment) or ""
    return ""


def summarize(rows: list[tuple[dt.datetime, float, float]], prev_close: float | None, market: str = "") -> dict[str, Any] | None:
    """분봉 → 화면용: 점들, 시가, 최고·최저(시각 포함), 마지막 값, 어제 종가 대비."""
    if not rows:
        return None
    hi = max(rows, key=lambda r: r[1])
    lo = min(rows, key=lambda r: r[1])
    last = rows[-1][1]
    return {
        "date": rows[0][0].date().isoformat(),
        # 시각, 가격, 거래량, 한국 시각, 정규장 밖 거래("nxt"/"pre"/"post", 정규장은 "")
        "points": [[r[0].strftime("%H:%M"), r[1], r[2], r[0].isoformat(timespec="minutes"), session_of(market, r[0])] for r in rows],
        "session": session_of(market, rows[-1][0]) or None,  # 마지막 가격이 정규장 밖 거래인가
        "open": rows[0][1],
        "last": last,
        "high": {"time": hi[0].strftime("%H:%M"), "price": hi[1]},
        "low": {"time": lo[0].strftime("%H:%M"), "price": lo[1]},
        "prev_close": prev_close,
        "change_pct": (last / prev_close - 1) * 100 if prev_close else None,
        "updated_at": rows[-1][0].isoformat(),
    }


def today(market: str, symbol: str, prev_close: float | None = None, yf_ticker: str | None = None) -> dict[str, Any] | None:
    """market: KRX / US / INDEX. 지표(INDEX)는 yf_ticker로 받는다. 어제 종가를 모르면 yfinance 값을 쓴다."""
    key = (market, symbol)
    now = dt.datetime.now()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    try:
        if market == Market.KRX.value:
            rows, yf_prev = _naver_minutes(symbol), None
        else:
            rows, yf_prev = _yf_minutes(yf_ticker or symbol, prepost=market == Market.US.value)
        result = summarize(rows, prev_close or yf_prev, market)
    except Exception as e:
        log.warning("분봉 조회 실패 %s: %s", symbol, e)
        result = None
    with _lock:
        _cache[key] = (now, result)
    return result
