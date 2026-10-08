import json
from datetime import datetime
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import ideas, plans, portfolio
from stockmate.db import make_engine
from stockmate.models import Broker, Market
from test_portfolio import FakePrices


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


BRK = {
    "name": "버크셔 해서웨이 B", "symbol": "brk.b", "account": "토스", "summary": "방어주",
    "thesis": ["자사주 매입", "현금 많음"], "risks": ["강세장에 덜 오름"], "overlap": "S&P500에 조금",
    "entry_low": None, "entry_high": "$505",
    "sources": [{"title": "기사", "url": "https://example.com/a"}, {"title": "나쁜 링크", "url": "javascript:alert(1)"}],
}  # fmt: skip


def test_split_ideas_block():
    text, data = ideas.split_ideas_block('답변\n```ideas\n[{"name": "x"}]\n```')
    assert text == "답변" and data == [{"name": "x"}]
    assert ideas.split_ideas_block('```ideas\n{"ideas": [{"name": "y"}]}\n```')[1] == [{"name": "y"}]
    assert ideas.split_ideas_block("```ideas\n[깨짐\n```")[1] is None


def test_apply_normalizes_symbol_and_keeps_only_web_links(session):
    [idea] = ideas.apply_ideas(session, [BRK, {"symbol": "X"}], "chat")
    assert idea.symbol == "BRK-B" and idea.market is Market.US and idea.entry_high == D(505)
    assert idea.thesis == "자사주 매입\n현금 많음"
    assert json.loads(idea.sources) == [{"title": "기사", "url": "https://example.com/a"}]


def test_same_symbol_replaces_old_idea(session):
    old = ideas.apply_ideas(session, [BRK], "chat", now=datetime(2026, 10, 1))[0]
    new = ideas.apply_ideas(session, [{**BRK, "symbol": "BRK-B", "summary": "새 요약"}], "analysis")[0]
    assert old.status == "replaced" and [i.id for i in ideas.active_ideas(session)] == [new.id]


def test_rows_track_price_since_recommendation_and_flags(session):
    [idea] = ideas.apply_ideas(session, [BRK], "chat")
    plans.apply_actions(session, {"add": [{"side": "buy", "name": "버크셔", "symbol": "BRK.B"}]}, "chat")
    rows = ideas.idea_rows(session, FakePrices({(Market.US, "BRK-B"): D(500)}))
    first = rows["active"][0]
    assert first["price_at"] == 500 and first["change_pct"] == 0 and first["in_zone"] is True
    assert first["planned"] is True and first["held"] is False

    # 다음 조회: 추천 때 가격은 그대로, 지금 가격만 바뀐다
    account = portfolio.create_account(session, "토스", Broker.TOSS)
    portfolio.set_holding(session, account, portfolio.get_or_create_instrument(session, "BRK-B"), D(1), D(500))
    second = ideas.idea_rows(session, FakePrices({(Market.US, "BRK-B"): D(550)}))["active"][0]
    assert second["price_at"] == 500 and round(second["change_pct"]) == 10
    assert second["in_zone"] is False and second["held"] is True


def test_dismiss_and_restore(session):
    [idea] = ideas.apply_ideas(session, [BRK], "chat")
    ideas.set_status(session, idea.id, "dismissed")
    rows = ideas.idea_rows(session, FakePrices({}))
    assert rows["active"] == [] and rows["dismissed"][0]["id"] == idea.id
    assert ideas.set_status(session, idea.id, "bogus") is None
    ideas.set_status(session, idea.id, "active")
    assert ideas.context_text(session).startswith("- ") and "BRK-B" in ideas.context_text(session)
