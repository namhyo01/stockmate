import threading
from datetime import timedelta
from decimal import Decimal as D

from stockmate.market_data import CachedPrices
from stockmate.models import Market

KEY = (Market.KRX, "005930")


class Counting:
    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    def last_prices(self, keys):
        self.calls += 1
        return {k: self.prices[k] for k in keys if k in self.prices}

    def fx_to_krw(self, currency):
        return D(1400)


def test_reuses_prices_within_ttl():
    inner = Counting({KEY: D(70000)})
    cache = CachedPrices(lambda: inner, ttl=timedelta(minutes=1))
    assert cache.last_prices([KEY]) == {KEY: D(70000)}
    assert cache.last_prices([KEY]) == {KEY: D(70000)}
    assert inner.calls == 1


def test_failed_symbols_are_retried():
    inner = Counting({})
    cache = CachedPrices(lambda: inner, ttl=timedelta(minutes=1))
    assert cache.last_prices([KEY]) == {}
    inner.prices[KEY] = D(70000)
    assert cache.last_prices([KEY]) == {KEY: D(70000)}


def test_invalidate_during_fetch_does_not_drop_prices():
    """새로고침(invalidate)이 시세 조회 도중에 들어와도 조회 결과가 비지 않아야 한다."""
    fetching, release = threading.Event(), threading.Event()

    class Slow(Counting):
        def last_prices(self, keys):
            fetching.set()
            release.wait(2)
            return super().last_prices(keys)

    cache = CachedPrices(lambda: Slow({KEY: D(70000)}), ttl=timedelta(minutes=1))
    result = {}
    worker = threading.Thread(target=lambda: result.update(cache.last_prices([KEY])))
    worker.start()
    fetching.wait(2)
    invalidator = threading.Thread(target=cache.invalidate)
    invalidator.start()
    release.set()
    worker.join(); invalidator.join()
    assert result == {KEY: D(70000)}


def test_cached_prices_keep_previous_closes_until_invalidated():
    class WithPrev(Counting):
        def previous_closes(self, keys):
            return {k: D(68000) for k in keys if k in self.prices}

    inner = WithPrev({KEY: D(70000)})
    cache = CachedPrices(lambda: inner, ttl=timedelta(minutes=1))
    cache.last_prices([KEY])
    assert cache.previous_closes([KEY]) == {KEY: D(68000)}
    cache.invalidate()
    assert cache.previous_closes([KEY]) == {}


def test_public_market_data_remembers_last_two_closes():
    from stockmate.market_data import PublicMarketData

    data, prices = PublicMarketData(), {}
    data._remember(prices, KEY, [D(68000), D(70000)])
    data._remember(prices, (Market.US, "NEW"), [D(10)])  # 상장 첫날: 전일 종가 없음
    assert prices == {KEY: D(70000), (Market.US, "NEW"): D(10)}
    assert data.previous_closes([KEY, (Market.US, "NEW")]) == {KEY: D(68000)}
