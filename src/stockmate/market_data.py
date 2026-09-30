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


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 4)))


class PublicMarketData:
    """국내는 FinanceDataReader, 미국과 환율은 yfinance를 쓴다."""

    def __init__(self) -> None:
        self._fx_cache: dict[str, Decimal] = {"KRW": Decimal(1)}

    def last_prices(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        keys = set(keys)
        prices: dict[Key, Decimal] = {}
        krx_symbols = sorted(symbol for market, symbol in keys if market is Market.KRX)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for symbol, price in zip(krx_symbols, pool.map(self._krx_price, krx_symbols)):
                if price is not None:
                    prices[(Market.KRX, symbol)] = price
        us_symbols = sorted(symbol for market, symbol in keys if market is Market.US)
        if us_symbols:
            prices.update({(Market.US, s): p for s, p in self._us_prices(us_symbols).items()})
        return prices

    def fx_to_krw(self, currency: str) -> Decimal:
        if currency not in self._fx_cache:
            if currency != "USD":
                raise MarketDataError(f"지원하지 않는 통화예요: {currency}")
            self._fx_cache[currency] = self._usd_krw()
        return self._fx_cache[currency]

    def _krx_price(self, symbol: str) -> Decimal | None:
        import FinanceDataReader as fdr

        start = dt.date.today() - dt.timedelta(days=14)
        try:
            closes = fdr.DataReader(symbol, start)["Close"].dropna()
        except Exception as e:
            log.warning("국내 시세 조회 실패 %s: %s", symbol, e)
            return None
        return None if closes.empty else _to_decimal(closes.iloc[-1])

    def _us_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        import yfinance as yf

        try:
            closes = yf.download(symbols, period="5d", progress=False, auto_adjust=False)["Close"]
        except Exception as e:
            log.warning("미국 시세 조회 실패 %s: %s", symbols, e)
            return {}
        result = {}
        for symbol in symbols:
            if symbol in closes and not (series := closes[symbol].dropna()).empty:
                result[symbol] = _to_decimal(series.iloc[-1])
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
                self.fetched_at = self.fetched_at or dt.datetime.now()
            return {k: self._prices[k] for k in keys if k in self._prices}

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
