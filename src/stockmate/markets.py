"""화면 위쪽 시장 지표 띠: 코스피·코스닥·S&P500·나스닥 등 지수, 환율, 금리, 비트코인.

국내외 지수·환율·금리는 네이버 증권(지연 없음, 장 상태 포함), 비트코인은 yfinance에서 가져온다.
여러 요청이 같이 부르므로 20초 동안 재사용하고, 바꿀 때는 잠금 안에서 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

_DOMESTIC = "https://m.stock.naver.com/api/index/{code}/basic"
_WORLD = "https://api.stock.naver.com/index/{code}/basic"
_MARKET_INDEX = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category={category}&reutersCode={code}"

# (키, 화면 이름, 출처, 코드, 단위)
INDICATORS = [
    ("kospi", "코스피", "domestic", "KOSPI", ""),
    ("kosdaq", "코스닥", "domestic", "KOSDAQ", ""),
    ("sp500", "S&P 500", "world", ".INX", ""),
    ("nasdaq", "나스닥", "world", ".IXIC", ""),
    ("dow", "다우존스", "world", ".DJI", ""),
    ("sox", "필라델피아 반도체", "world", ".SOX", ""),
    ("nikkei", "니케이 225", "world", ".N225", ""),
    ("usdkrw", "원/달러", "exchange", "FX_USDKRW", "원"),
    ("us10y", "미국 10년물", "bond", "US10YT=RR", "%"),
    ("btc", "비트코인", "yfinance", "BTC-USD", "$"),
]


@dataclass
class Quote:
    key: str
    label: str
    value: float
    change: float | None
    change_pct: float | None
    status: str | None  # OPEN | CLOSE | PREOPEN ... (출처 그대로)
    at: str | None
    unit: str


def _number(text: Any) -> float | None:
    if text in (None, ""):
        return None
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=6) as response:
        return json.load(response)


def parse_naver(key: str, label: str, unit: str, data: dict[str, Any]) -> Quote | None:
    """네이버 지수·시장지표 응답 → Quote. 지수는 compareToPreviousClosePrice, 환율·금리는 fluctuations."""
    data = data.get("result", data)
    value = _number(data.get("closePrice"))
    if value is None:
        return None
    change = _number(data.get("compareToPreviousClosePrice", data.get("fluctuations")))
    return Quote(key, label, value, change, _number(data.get("fluctuationsRatio")), data.get("marketStatus"),
                 data.get("localTradedAt"), unit)  # fmt: skip


def _fetch(item: tuple[str, str, str, str, str]) -> Quote | None:
    key, label, source, code, unit = item
    try:
        if source == "yfinance":
            import yfinance as yf

            info = yf.Ticker(code).fast_info
            value, prev = float(info["last_price"]), float(info["previous_close"])
            change = value - prev
            return Quote(key, label, value, change, change / prev * 100 if prev else None, "OPEN", None, unit)
        if source == "domestic":
            url = _DOMESTIC.format(code=code)
        elif source == "world":
            url = _WORLD.format(code=code)
        else:
            url = _MARKET_INDEX.format(category=source, code=code)
        return parse_naver(key, label, unit, _get_json(url))
    except Exception as e:  # 하나가 실패해도 나머지는 보여준다
        log.warning("시장 지표 조회 실패 %s: %s", code, e)
        return None


class MarketBoard:
    def __init__(self, ttl: dt.timedelta = dt.timedelta(seconds=20), fetch=_fetch):
        self._ttl = ttl
        self._fetch = fetch
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._fetched_at: dt.datetime | None = None

    def get(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._fetched_at is None or dt.datetime.now() - self._fetched_at > self._ttl:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    fresh = {q.key: q for q in pool.map(self._fetch, INDICATORS) if q is not None}
                self._quotes.update(fresh)  # 이번에 실패한 지표는 지난 값을 그대로 둔다
                self._fetched_at = dt.datetime.now()
            return [asdict(self._quotes[k]) for k, *_ in INDICATORS if k in self._quotes]


board = MarketBoard()
