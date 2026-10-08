"""시세·환율·종목명 조회.

무료 공개 데이터를 쓰므로 실시간 시세가 아니라 최근 종가(장중에는 지연 시세) 기준이다.
한국투자증권 API를 연동하면(2단계) 실시간 시세 제공자로 교체할 수 있도록 PriceProvider로 추상화했다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Protocol

from stockmate.models import Market

log = logging.getLogger(__name__)

Key = tuple[Market, str]


class MarketDataError(Exception):
    pass


class PriceProvider(Protocol):
    def last_prices(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        """조회에 성공한 종목의 현재가만 돌려준다 (종목 통화 기준)."""
        ...

    def fx_to_krw(self, currency: str) -> Decimal:
        """1 단위 통화가 몇 원인지 돌려준다."""
        ...


def previous_closes(provider: object, keys: Iterable[Key]) -> dict[Key, Decimal]:
    """전일 종가 (오늘 등락 계산용). 제공자가 지원하지 않으면 빈 dict.

    last_prices()를 부를 때 함께 받아 둔 값을 돌려주므로 last_prices() 다음에 부른다.
    """
    method = getattr(provider, "previous_closes", None)
    return method(keys) if method else {}


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 4)))


class PublicMarketData:
    """국내는 FinanceDataReader, 미국과 환율은 yfinance를 쓴다."""

    def __init__(self) -> None:
        self._fx_cache: dict[str, Decimal] = {"KRW": Decimal(1)}
        self._prev: dict[Key, Decimal] = {}

    def last_prices(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        keys = set(keys)
        prices: dict[Key, Decimal] = {}
        krx_symbols = sorted(symbol for market, symbol in keys if market is Market.KRX)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for symbol, closes in zip(krx_symbols, pool.map(self._krx_closes, krx_symbols)):
                self._remember(prices, (Market.KRX, symbol), closes)
        us_symbols = sorted(symbol for market, symbol in keys if market is Market.US)
        if us_symbols:
            for symbol, closes in self._us_closes(us_symbols).items():
                self._remember(prices, (Market.US, symbol), closes)
        return prices

    def previous_closes(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        return {k: self._prev[k] for k in keys if k in self._prev}

    def _remember(self, prices: dict[Key, Decimal], key: Key, closes: list[Decimal]) -> None:
        """최근 종가 목록에서 현재가(마지막)와 전일 종가(그 앞)를 기억한다."""
        if closes:
            prices[key] = closes[-1]
        if len(closes) >= 2:
            self._prev[key] = closes[-2]

    def fx_to_krw(self, currency: str) -> Decimal:
        if currency not in self._fx_cache:
            if currency != "USD":
                raise MarketDataError(f"지원하지 않는 통화예요: {currency}")
            self._fx_cache[currency] = self._usd_krw()
        return self._fx_cache[currency]

    def _krx_closes(self, symbol: str) -> list[Decimal]:
        import FinanceDataReader as fdr

        start = dt.date.today() - dt.timedelta(days=14)
        try:
            closes = fdr.DataReader(symbol, start)["Close"].dropna()
        except Exception as e:
            log.warning("국내 시세 조회 실패 %s: %s", symbol, e)
            return []
        return [_to_decimal(v) for v in closes.iloc[-2:]]

    def _us_closes(self, symbols: list[str]) -> dict[str, list[Decimal]]:
        import yfinance as yf

        try:
            closes = yf.download(symbols, period="5d", progress=False, auto_adjust=False)["Close"]
        except Exception as e:
            log.warning("미국 시세 조회 실패 %s: %s", symbols, e)
            return {}
        result = {}
        for symbol in symbols:
            if symbol in closes and not (series := closes[symbol].dropna()).empty:
                result[symbol] = [_to_decimal(v) for v in series.iloc[-2:]]
        return result

    def _usd_krw(self) -> Decimal:
        import FinanceDataReader as fdr
        import yfinance as yf

        start = dt.date.today() - dt.timedelta(days=14)
        sources = [
            lambda: yf.Ticker("KRW=X").history(period="5d")["Close"],
            lambda: fdr.DataReader("USD/KRW", start)["Close"],
        ]
        for fetch in sources:
            try:
                closes = fetch().dropna()
            except Exception as e:
                log.warning("환율 조회 실패: %s", e)
                continue
            if not closes.empty:
                return _to_decimal(closes.iloc[-1])
        raise MarketDataError("원/달러 환율을 가져오지 못했어요.")


class CachedPrices:
    """시세와 환율을 ttl 동안 재사용한다. 웹 대시보드가 열릴 때마다 외부 조회를 반복하지 않기 위해 쓴다.

    여러 요청 스레드가 동시에 쓰므로 모든 상태 변경은 잠금 안에서 한다.
    가격을 못 가져온 종목은 캐시하지 않고 다음 요청에서 다시 시도한다.
    """

    def __init__(self, factory: Callable[[], PriceProvider], ttl: dt.timedelta):
        self._factory = factory
        self._ttl = ttl
        self._lock = threading.RLock()
        self.invalidate()

    def invalidate(self) -> None:
        with self._lock:
            self._inner = self._factory()
            self._prices: dict[Key, Decimal] = {}
            self._prev: dict[Key, Decimal] = {}
            self._fx: dict[str, Decimal] = {}
            self.fetched_at: dt.datetime | None = None

    def _refresh_if_expired(self) -> None:
        if self.fetched_at and dt.datetime.now() - self.fetched_at > self._ttl:
            self.invalidate()

    def last_prices(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        keys = set(keys)
        with self._lock:
            self._refresh_if_expired()
            if missing := keys - self._prices.keys():
                self._prices.update(self._inner.last_prices(missing))
                self._prev.update(previous_closes(self._inner, missing))
                self.fetched_at = self.fetched_at or dt.datetime.now()
            return {k: self._prices[k] for k in keys if k in self._prices}

    def previous_closes(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        with self._lock:
            return {k: self._prev[k] for k in keys if k in self._prev}

    def fx_to_krw(self, currency: str) -> Decimal:
        with self._lock:
            self._refresh_if_expired()
            if currency not in self._fx:
                self._fx[currency] = self._inner.fx_to_krw(currency)
            return self._fx[currency]


def lookup_name(symbol: str, market: Market) -> str | None:
    """종목명을 찾는다. 실패하면 None (종목명은 필수가 아니므로 조용히 넘어간다)."""
    try:
        if market is Market.KRX:
            request = urllib.request.Request(
                f"https://m.stock.naver.com/api/stock/{symbol}/basic",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response).get("stockName")

        import yfinance as yf

        return yf.Ticker(symbol).info.get("shortName")
    except Exception as e:
        log.warning("종목명 조회 실패 %s: %s", symbol, e)
        return None
