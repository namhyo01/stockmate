from collections.abc import Iterable
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import portfolio
from stockmate.db import make_engine
from stockmate.market_data import Key
from stockmate.models import AccountKind, Broker, Market, Side
from stockmate.portfolio import PortfolioError


class FakePrices:
    def __init__(self, prices: dict[Key, D], usd_krw: D = D(1400)):
        self.prices = prices
        self.usd_krw = usd_krw

    def last_prices(self, keys: Iterable[Key]) -> dict[Key, D]:
        return {k: self.prices[k] for k in keys if k in self.prices}

    def fx_to_krw(self, currency: str) -> D:
        return D(1) if currency == "KRW" else self.usd_krw


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


@pytest.fixture
def toss(session):
    return portfolio.create_account(session, "토스", Broker.TOSS)


def buy(session, account, symbol, qty, price, fee="0"):
    instrument = portfolio.get_or_create_instrument(session, symbol)
    return portfolio.record_trade(
        session, account, instrument, Side.BUY, D(qty), D(price), date(2026, 9, 1), fee=D(fee)
    )


def sell(session, account, symbol, qty, price, fee="0"):
    instrument = portfolio.get_or_create_instrument(session, symbol)
    return portfolio.record_trade(
        session, account, instrument, Side.SELL, D(qty), D(price), date(2026, 9, 2), fee=D(fee)
    )


def holding_of(account, symbol):
    return next((h for h in account.holdings if h.instrument.symbol == symbol), None)


@pytest.mark.parametrize(
    ("symbol", "market"),
    [("005930", Market.KRX), ("0088M0", Market.KRX), ("AAPL", Market.US), ("brk-b", Market.US)],
)
def test_detect_market(symbol, market):
    assert portfolio.detect_market(symbol) is market


def test_duplicate_account_name_rejected(session, toss):
    with pytest.raises(PortfolioError):
        portfolio.create_account(session, "토스", Broker.KIS)


def test_get_account_by_name_or_id(session, toss):
    assert portfolio.get_account(session, "토스") is toss
    assert portfolio.get_account(session, str(toss.id)) is toss
    with pytest.raises(PortfolioError):
        portfolio.get_account(session, "없는계좌")


def test_buys_use_moving_average_cost(session, toss):
    buy(session, toss, "005930", "10", "70000")
    buy(session, toss, "005930", "10", "80000", fee="150")

    holding = holding_of(toss, "005930")
    assert holding.quantity == D(20)
    assert holding.avg_cost == D(75000)  # 평균단가에는 수수료를 넣지 않는다


def test_sell_records_realized_pnl_and_keeps_avg_cost(session, toss):
    buy(session, toss, "AAPL", "4", "150")
    trade = sell(session, toss, "aapl", "1.5", "200", fee="1")

    assert trade.realized_pnl == D("74")  # (200 - 150) * 1.5 - 1
    holding = holding_of(toss, "AAPL")
    assert holding.quantity == D("2.5")
    assert holding.avg_cost == D(150)


def test_selling_everything_removes_holding(session, toss):
    buy(session, toss, "005930", "3", "70000")
    sell(session, toss, "005930", "3", "71000")
    assert holding_of(toss, "005930") is None


def test_cannot_sell_more_than_held(session, toss):
    buy(session, toss, "005930", "3", "70000")
    with pytest.raises(PortfolioError):
        sell(session, toss, "005930", "4", "71000")


@pytest.mark.parametrize(("qty", "price"), [("0", "100"), ("-1", "100"), ("1", "-5")])
def test_invalid_trade_rejected(session, toss, qty, price):
    with pytest.raises(PortfolioError):
        buy(session, toss, "005930", qty, price)


def test_set_holding_overwrites_and_zero_deletes(session, toss):
    instrument = portfolio.get_or_create_instrument(session, "005930")
    portfolio.set_holding(session, toss, instrument, D(5), D(60000))
    portfolio.set_holding(session, toss, instrument, D(7), D(65000))
    assert holding_of(toss, "005930").quantity == D(7)

    portfolio.set_holding(session, toss, instrument, D(0), D(0))
    assert holding_of(toss, "005930") is None


def test_positions_are_valued_in_krw_across_accounts(session, toss):
    kis = portfolio.create_account(session, "한투", Broker.KIS)
    buy(session, toss, "005930", "10", "70000")
    buy(session, kis, "AAPL", "2", "150")
    buy(session, kis, "MSFT", "1", "400")  # 시세 조회 실패 가정
    prices = FakePrices({(Market.KRX, "005930"): D(77000), (Market.US, "AAPL"): D(200)})

    positions = portfolio.build_positions(session, prices)
    by_symbol = {p.instrument.symbol: p for p in positions}

    samsung = by_symbol["005930"]
    assert samsung.value_krw == D(770000)
    assert samsung.pnl_krw == D(70000)
    assert samsung.pnl_pct == D(10)

    apple = by_symbol["AAPL"]
    assert apple.cost_krw == D(420000)  # 2 * 150 * 1400
    assert apple.value_krw == D(560000)  # 2 * 200 * 1400

    assert by_symbol["MSFT"].value_krw is None

    grand = portfolio.totals(positions)
    assert grand.count == 2  # 현재가 없는 MSFT는 합계에서 빠진다
    assert grand.cost_krw == D(1120000)
    assert grand.value_krw == D(1330000)

    by_broker = portfolio.group_totals(positions, lambda p: p.account.broker)
    assert by_broker[Broker.TOSS].value_krw == D(770000)
    assert by_broker[Broker.KIS].value_krw == D(560000)


def test_pension_account_with_monthly_contribution(session):
    account = portfolio.create_account(
        session, "삼성연금", Broker.SAMSUNG, AccountKind.PENSION, D(400000)
    )
    assert account.kind is AccountKind.PENSION
    assert account.monthly_contribution == D(400000)
    with pytest.raises(PortfolioError):
        portfolio.create_account(session, "음수", Broker.SAMSUNG, monthly_contribution=D(-1))


def test_blend_avg_fx_keeps_krw_cost_basis(session, toss):
    nvda = portfolio.get_or_create_instrument(session, "NVDA")
    assert portfolio.blend_avg_fx(None, D(1), D(100), D(1400)) == D(1400)  # 처음 보면 지금 환율

    holding = portfolio.set_holding(session, toss, nvda, D(2), D(100), D(1300))  # 원화 원금 260,000
    # 같은 수량: 원화 원금 보존 (달러 평단 표기가 바뀌어도)
    assert portfolio.blend_avg_fx(holding, D(2), D(104), D(1450)) == D(1250)
    # 1주 더 삼(120달러, 지금 환율 1500): 260,000 + 120 × 1500 = 440,000원 / 320달러
    assert portfolio.blend_avg_fx(holding, D(3), D("106.666667"), D(1500)).quantize(D("0.1")) == D("1375.0")
    # 1주 팖: 평균 환율 그대로
    assert portfolio.blend_avg_fx(holding, D(1), D(100), D(1500)) == D(1300)


def test_position_cost_uses_purchase_fx(session, toss):
    nvda = portfolio.get_or_create_instrument(session, "NVDA")
    portfolio.set_holding(session, toss, nvda, D(1), D(100), D(1300))
    portfolio.set_holding(session, toss, nvda, D(1), D(110))  # avg_fx를 안 주면 기존 값 유지
    [p] = portfolio.build_positions(session, FakePrices({(Market.US, "NVDA"): D(120)}, usd_krw=D(1400)))
    assert p.cost_krw == D(110) * 1300
    assert p.value_krw == D(120) * 1400
    assert p.pnl_krw == D(168000) - D(143000)  # 주가 + 환율 손익


class FakeQuotes(FakePrices):
    """전일 종가도 주는 시세 제공자."""

    def __init__(self, prices, prev, usd_krw=D(1400)):
        super().__init__(prices, usd_krw)
        self.prev = prev

    def previous_closes(self, keys):
        return {k: self.prev[k] for k in keys if k in self.prev}


def test_day_change_per_position_and_totals(session, toss):
    for symbol, qty in [("005930", "10"), ("AAPL", "2"), ("000660", "1")]:
        instrument = portfolio.get_or_create_instrument(session, symbol)
        portfolio.set_holding(session, toss, instrument, D(qty), D(1))
    provider = FakeQuotes(
        {(Market.KRX, "005930"): D(275), (Market.US, "AAPL"): D(99), (Market.KRX, "000660"): D(500)},
        {(Market.KRX, "005930"): D(250), (Market.US, "AAPL"): D(100)},  # 하이닉스는 전일 종가를 모름
        usd_krw=D(1000),
    )
    positions = {p.instrument.symbol: p for p in portfolio.build_positions(session, provider)}

    assert positions["005930"].day_change_pct == D(10)
    assert positions["005930"].day_pnl_krw == D(250)
    assert positions["AAPL"].day_change_pct == D(-1)
    assert positions["AAPL"].day_pnl_krw == D(-2000)  # (99 − 100) × 2주 × 1000원
    assert positions["000660"].day_change_pct is None

    total = portfolio.totals(positions.values())
    assert total.day_pnl_krw == D(-1750)
    # 어제 평가금 = 지금 평가금 − 오늘 변화 (모르는 종목은 변화 0으로 본다)
    assert total.day_pct == D(-1750) / (D(2750 + 198000 + 500) + 1750) * 100


def test_provider_without_previous_closes_has_no_day_change(session, toss):
    instrument = portfolio.get_or_create_instrument(session, "005930")
    portfolio.set_holding(session, toss, instrument, D(1), D(1))
    [p] = portfolio.build_positions(session, FakePrices({(Market.KRX, "005930"): D(10)}))
    assert p.day_change_pct is None and p.day_pnl_krw is None
    assert portfolio.totals([p]).day_pnl_krw == 0
