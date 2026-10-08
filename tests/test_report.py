from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import portfolio
from stockmate.db import make_engine
from stockmate.models import AccountKind, Broker
from stockmate.report import build_snapshot
from test_portfolio import FakePrices


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


def test_cash_keeps_krw_and_usd_and_converts_at_current_rate(session):
    toss = portfolio.create_account(session, "토스", Broker.TOSS)
    toss.cash, toss.cash_usd = D(1_000_000), D(100)
    isa = portfolio.create_account(session, "한투", Broker.KIS, AccountKind.ISA)
    isa.cash = D(50_000)
    portfolio.create_account(session, "삼성연금", Broker.SAMSUNG, AccountKind.PENSION)

    snap = build_snapshot(session, FakePrices({}, usd_krw=D(1400)))
    accounts = {a["name"]: a for a in snap["accounts"]}
    assert accounts["토스"]["cash"] == 1_140_000 and accounts["토스"]["cash_usd"] == 100
    assert accounts["한투"]["cash"] == 50_000 and accounts["한투"]["cash_usd"] is None
    assert accounts["삼성연금"]["cash"] is None
    assert snap["total"]["cash"] == 1_190_000 and snap["fx"] == {"USD": 1400}
