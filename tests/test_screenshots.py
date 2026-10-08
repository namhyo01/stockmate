import json
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import importers, portfolio, screenshots
from stockmate.db import make_engine
from stockmate.models import Broker, Market
from test_portfolio import FakePrices


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


@pytest.fixture
def toss(session):
    account = portfolio.create_account(session, "토스", Broker.TOSS)
    for symbol, qty, cost in [("005930", "10", "71000"), ("AAPL", "1", "200"), ("QLD", "1", "100")]:
        instrument = portfolio.get_or_create_instrument(session, symbol)
        portfolio.set_holding(session, account, instrument, D(qty), D(cost))
    return account


def holding(symbol, name, qty, value, pnl, currency="KRW", market=None, **extra):
    return {
        "name": name, "symbol": symbol, "market": market or portfolio.detect_market(symbol).value,
        "quantity": D(qty), "avg_cost": None, "avg_cost_currency": None,
        "value": D(value), "pnl": D(pnl), "amount_currency": currency, **extra,
    }  # fmt: skip


PRICES = FakePrices(
    {(Market.KRX, "005930"): D(80000), (Market.US, "AAPL"): D(250), (Market.US, "NVDA"): D(180)},
    usd_krw=D(1400),
)


def test_derives_avg_cost_from_value_and_pnl_with_screen_fx(toss):
    extracted = {
        "holdings": [
            # 토스 화면: 원화 평가금과 손익만 있다. 평단 = (평가금 − 손익) / 환율 / 수량
            holding("AAPL", "애플", "2", "700000", "140000"),
            holding("005930", "삼성전자", "10", "800000", "90000"),
        ],
        "usd_krw": D(1400),
    }
    draft = screenshots.build_draft(toss, extracted, PRICES)
    aapl, samsung = draft.rows

    assert aapl.avg_cost == D(200) and aapl.avg_cost_source == "derived"
    assert aapl.status == "changed" and aapl.before_quantity == D(1)
    assert samsung.avg_cost == D(71000) and samsung.status == "same"
    assert str(samsung.avg_cost) == "71000"  # 7.1E+4 같은 지수 표기가 아니어야 한다
    assert aapl.ok and samsung.ok
    assert [r["symbol"] for r in draft.removed] == ["QLD"]


def test_screen_avg_cost_wins_and_is_converted_to_instrument_currency(toss):
    row = holding("AAPL", "애플", "1", "350000", "0")
    row.update(avg_cost=D(280000), avg_cost_currency="KRW")
    draft = screenshots.build_draft(toss, {"holdings": [row], "usd_krw": D(1400)}, PRICES)
    assert draft.rows[0].avg_cost == D(200) and draft.rows[0].avg_cost_source == "screen"


def test_flags_wrong_ticker_by_price_gap_and_missing_quote(toss):
    extracted = {
        "holdings": [
            holding("NVDA", "엔비디아", "1", "700000", "0"),  # 500달러로 보이는데 시세는 180달러
            holding("MBRXX", "멀레큘린", "1", "14000", "0"),  # 없는 티커
        ],
        "usd_krw": D(1400),
    }
    nvda, unknown = screenshots.build_draft(toss, extracted, PRICES).rows
    assert not nvda.ok and "달라요" in nvda.warnings[0]
    assert nvda.price_gap_pct > 100
    assert not unknown.ok and "시세를 찾지 못했어요" in unknown.warnings[0]


def test_duplicates_across_screenshots_count_once_and_coverage_check(toss):
    extracted = {
        "holdings": [
            holding("005930", "삼성전자", "10", "800000", "90000"),
            holding("005930", "삼성전자", "10", "800000", "90000"),
            holding(None, "알 수 없는 종목", "1", "1000", "0", market="OTHER"),
        ],
        "total_value": D(2_000_000),
        "total_value_currency": "KRW",
    }
    draft = screenshots.build_draft(toss, extracted, PRICES)
    assert [r.name for r in draft.rows] == ["삼성전자", "알 수 없는 종목"]
    assert draft.rows_total_krw == D(801000)
    assert "빠진 종목" in draft.coverage_warning
    other = draft.rows[1]
    assert not other.ok and other.market is None


def test_draft_to_dict_is_json_serializable(toss):
    draft = screenshots.build_draft(
        toss, {"holdings": [holding("AAPL", "애플", "0.5", "175000", "35000")], "usd_krw": D(1400)}, PRICES
    )
    data = json.loads(json.dumps(screenshots.draft_to_dict(draft)))
    assert data["rows"][0]["avg_cost"] == "200"
    assert data["rows"][0]["quantity"] == "0.5"


def test_apply_only_removes_what_was_asked(session, toss):
    rows = [importers.HoldingRow("AAPL", D(2), D(200), "애플", Market.US)]
    result = screenshots.apply_rows(
        session, toss, rows, remove=[(Market.US, "QLD"), (Market.US, "AAPL")], cash_krw=D(50000)
    )
    held = {h.instrument.symbol: h for h in toss.holdings}
    assert set(held) == {"005930", "AAPL"}  # 체크를 푼 삼성전자는 그대로, QLD만 지운다
    assert held["AAPL"].quantity == D(2)
    assert result.removed == ["QLD"] and toss.cash == D(50000)


def test_parse_result_reads_last_result_line_with_decimals():
    stdout = "\n".join(
        [
            json.dumps({"type": "system"}),
            json.dumps({"type": "result", "is_error": False, "result": '{"holdings": [], "usd_krw": 1395.55}'}),
        ]
    )
    assert screenshots._parse_result(stdout)["usd_krw"] == D("1395.55")

    error = json.dumps({"type": "result", "is_error": True, "result": "로그인 필요"})
    with pytest.raises(screenshots.ScreenshotError, match="로그인 필요"):
        screenshots._parse_result(error)


def test_mmf_becomes_cash_and_missing_quantity_is_estimated(session):
    # 삼성증권 연금 잔고 화면: 수량·평단 없이 평가손익·평가금액·매수금액만 보이고, 대기 자금은 MMF로 들어 있다
    pension = portfolio.create_account(session, "삼성연금", Broker.SAMSUNG)
    extracted = {
        "holdings": [
            {**holding("360750", "TIGER 미국S&P500", "1", "182070", "70"), "quantity": None},
            {**holding(None, "삼성신종종류형MMF제4호-CP", "1", "218023", "0", market="OTHER"), "quantity": None},
        ],
        "total_value": D(400093),
        "total_value_currency": "KRW",
    }
    prices = FakePrices({(Market.KRX, "360750"): D(26010)})
    draft = screenshots.build_draft(pension, extracted, prices)

    [snp] = draft.rows
    assert snp.quantity == D(7) and snp.quantity_source == "estimated"
    assert snp.avg_cost == D(26000)  # 매수금액 182,000 ÷ 7주
    assert snp.ok
    assert draft.cash_krw == D(218023) and draft.cash_like[0]["name"].startswith("삼성신종")
    assert draft.coverage_warning is None  # MMF까지 더하면 화면 총평가금액과 맞는다


def test_quantity_is_not_guessed_when_far_from_whole_number(toss):
    row = {**holding("005930", "삼성전자", "1", "400000", "0"), "quantity": None}
    draft = screenshots.build_draft(toss, {"holdings": [row]}, PRICES)  # 400,000 ÷ 80,000 = 5주 → 맞음
    assert draft.rows[0].quantity == D(5)
    odd = {**holding("005930", "삼성전자", "1", "420000", "0"), "quantity": None}  # 5.25주 → 추정하지 않음
    draft = screenshots.build_draft(toss, {"holdings": [odd]}, PRICES)
    assert draft.rows[0].quantity is None and "수량을 읽지 못했어요" in draft.rows[0].warnings
