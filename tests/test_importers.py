from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import importers, portfolio
from stockmate.db import make_engine
from stockmate.models import Broker, Market
from stockmate.portfolio import PortfolioError


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


def test_parse_template():
    rows = importers.parse_holdings_csv(importers.TEMPLATE)
    assert rows == [
        importers.HoldingRow("005930", D(10), D(71000), "삼성전자", Market.KRX),
        importers.HoldingRow("AAPL", D("3.5"), D("180.25"), "Apple", Market.US),
    ]


def test_parse_handles_bom_thousands_separators_and_blank_rows():
    text = '﻿symbol,quantity,avg_cost\n005930,"1,000","71,500"\n,,\n'
    rows = importers.parse_holdings_csv(text)
    assert rows == [importers.HoldingRow("005930", D(1000), D(71500))]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("symbol,quantity\n005930,1\n", "avg_cost"),
        ("symbol,quantity,avg_cost\n005930,abc,1\n", "2번째 줄"),
        ("symbol,quantity,avg_cost,market\n005930,1,1,JP\n", "KRX 또는 US"),
    ],
)
def test_parse_errors(text, message):
    with pytest.raises(PortfolioError, match=message):
        importers.parse_holdings_csv(text)


def test_import_merge_keeps_existing_and_replace_removes_them(session):
    account = portfolio.create_account(session, "삼성", Broker.SAMSUNG)
    existing = portfolio.get_or_create_instrument(session, "000660", name="SK하이닉스")
    portfolio.set_holding(session, account, existing, D(1), D(100000))
    rows = importers.parse_holdings_csv(importers.TEMPLATE)

    result = importers.import_holdings(session, account, rows)
    assert result.removed == []
    assert {h.instrument.symbol for h in account.holdings} == {"000660", "005930", "AAPL"}

    result = importers.import_holdings(session, account, rows, replace=True)
    assert [i.symbol for i in result.removed] == ["000660"]
    assert {h.instrument.symbol for h in account.holdings} == {"005930", "AAPL"}
