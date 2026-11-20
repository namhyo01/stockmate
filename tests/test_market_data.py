import datetime as dt
import threading
from datetime import timedelta
from decimal import Decimal as D

from stockmate import market_data
from stockmate.market_data import CachedPrices, ExtendedHours, us_regular, us_session_of
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


# ── 미국 프리·애프터마켓 ─────────────────────────

NY = market_data.NEW_YORK
NVDA, SAMSUNG = (Market.US, "NVDA"), (Market.KRX, "005930")


class Regular:
    """한투처럼 정규장 가격과 그 전 종가만 주는 제공자."""

    def last_prices(self, keys):
        return {k: v for k, v in {NVDA: D("146"), SAMSUNG: D("70000")}.items() if k in set(keys)}

    def previous_closes(self, keys):
        return {k: v for k, v in {NVDA: D("150"), SAMSUNG: D("69000")}.items() if k in set(keys)}

    def fx_to_krw(self, currency):
        return D("1400")


def at(*args):
    return dt.datetime(*args, tzinfo=NY)


def test_us_regular_hours_in_new_york_time():
    assert us_regular(at(2026, 11, 19, 9, 30)) and us_regular(at(2026, 11, 19, 15, 59))
    assert not us_regular(at(2026, 11, 19, 16, 0)) and not us_regular(at(2026, 11, 19, 9, 29))
    assert not us_regular(at(2026, 11, 21, 12, 0))  # 토요일
    assert us_session_of(at(2026, 11, 19, 8, 0)) == "pre" and us_session_of(at(2026, 11, 19, 19, 30)) == "post"
    assert us_session_of(at(2026, 11, 19, 12, 0)) is None
    assert us_session_of(at(2026, 11, 19, 20, 30)) == "day" and us_session_of(at(2026, 11, 20, 3, 0)) == "day"  # 한국 낮


def test_after_market_price_replaces_regular_close():
    provider = ExtendedHours(Regular(), extended=lambda symbols: {"NVDA": (D("140"), "post")}, now=lambda: at(2026, 11, 19, 19, 30))
    prices = provider.last_prices([NVDA, SAMSUNG])
    assert prices == {NVDA: D("140"), SAMSUNG: D("70000")}  # 국내는 그대로
    # 오늘 등락은 토스처럼 그날 정규장 종가 대비 (상장 첫날이면 전일 종가가 공모가라 크게 틀렸다)
    assert provider.previous_closes([NVDA, SAMSUNG]) == {NVDA: D("146"), SAMSUNG: D("69000")}
    assert provider.sessions([NVDA, SAMSUNG]) == {NVDA: "post"}


def test_pre_market_change_is_against_last_regular_close():
    provider = ExtendedHours(Regular(), extended=lambda symbols: {"NVDA": (D("148"), "pre")}, now=lambda: at(2026, 11, 20, 8, 0))
    assert provider.last_prices([NVDA]) == {NVDA: D("148")}
    assert provider.previous_closes([NVDA]) == {NVDA: D("146")}  # 지난 정규장 종가 대비


def test_regular_session_does_not_ask_for_extended_prices():
    def boom(symbols):
        raise AssertionError("정규장에는 부르지 않아요")

    provider = ExtendedHours(Regular(), extended=boom, now=lambda: at(2026, 11, 19, 11, 0))
    assert provider.last_prices([NVDA]) == {NVDA: D("146")} and provider.sessions([NVDA]) == {}


def test_no_extended_trade_keeps_regular_price():
    provider = ExtendedHours(Regular(), extended=lambda symbols: {}, now=lambda: at(2026, 11, 19, 19, 30))
    assert provider.last_prices([NVDA]) == {NVDA: D("146")}
    assert provider.previous_closes([NVDA]) == {NVDA: D("150")} and provider.sessions([NVDA]) == {}


def test_extended_prices_cached_per_symbol(monkeypatch):
    monkeypatch.setattr(market_data, "_ext_cache", {})
    calls = []

    def download(symbols):
        calls.append(list(symbols))
        return {"NVDA": (D("140"), "post")}

    assert market_data.extended_prices(["NVDA", "AAPL"], download) == {"NVDA": (D("140"), "post")}
    assert market_data.extended_prices(["NVDA", "AAPL", "MSFT"], download) == {"NVDA": (D("140"), "post")}
    assert calls == [["NVDA", "AAPL"], ["MSFT"]]  # 60초 안에는 받은 종목(못 받은 것도)을 다시 묻지 않는다


def test_cached_prices_pass_sessions_through():
    cached = CachedPrices(lambda: ExtendedHours(Regular(), extended=lambda s: {"NVDA": (D("140"), "post")},
                                                now=lambda: at(2026, 11, 19, 19, 30)), ttl=timedelta(seconds=60))  # fmt: skip
    assert cached.last_prices([NVDA]) == {NVDA: D("140")}
    assert cached.sessions([NVDA]) == {NVDA: "post"} and cached.previous_closes([NVDA]) == {NVDA: D("146")}


def test_extended_download_prefers_toss_and_falls_back_to_yfinance():
    from stockmate import sync

    class Toss:
        def prices(self, symbols):
            return {"ANT": (D("93.42"), at(2026, 11, 19, 20, 7)),  # 데이마켓
                    "VOO": (D("658"), at(2026, 11, 19, 15, 59))}  # 정규장 뒤 거래 없음  # fmt: skip

    asked = []

    def yf(symbols):
        asked.append(symbols)
        return {"JOBY": (D("3.9"), "post")}

    got = sync.extended_download(["ANT", "VOO", "JOBY"], toss=Toss(), fallback=yf)
    assert got == {"ANT": (D("93.42"), "day"), "JOBY": (D("3.9"), "post")}
    assert asked == [["JOBY"]]  # 토스가 답한 종목은 yfinance에 묻지 않는다

    class Broken:
        def prices(self, symbols):
            raise sync.TossError("막힘")

    assert sync.extended_download(["ANT"], toss=Broken(), fallback=lambda s: {"ANT": (D("92.6"), "post")}) == {"ANT": (D("92.6"), "post")}
