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


def test_plan_draft_from_idea(session):
    idea = ideas.apply_ideas(session, [{**BRK, "entry_low": 480, "entry_high": "505", "stop": 455, "target": "560.5"}], "weekly")[0]
    plans.apply_actions(session, {"add": [{"side": "buy", "name": "버크셔", "symbol": "BRK-B", "account": "토스", "quantity": 1}]}, "chat")
    d = ideas.plan_draft(session, idea)
    item = d["items"][0]
    assert (d["market"], d["symbol"], d["currency"]) == ("US", "BRK-B", "USD")
    assert (item["side"], item["account"], item["price_low"], item["price_high"]) == ("buy", "토스", 480.0, 505.0)
    assert item["timing"] == "추천 가격대에 들어오면 · 철회 기준 455 · 목표가 560.5"
    assert [o["side"] for o in d["open"]] == ["buy"]  # 열린 매수 계획은 바뀐다고 알려준다


def test_trade_math_reward_risk_ratio():
    m = ideas.trade_math(D(95), D(105), D(130), D(88))  # 가운데 100에서 +30% / −12%
    assert (m["base"], m["reward_pct"], m["risk_pct"]) == (100.0, 30.0, 12.0) and round(m["rr"], 2) == 2.5
    assert ideas.trade_math(None, None, D(110), None, price=D(100))["reward_pct"] == 10.0  # 사는 가격이 없으면 지금 가격
    assert ideas.trade_math(None, None, None, None)["rr"] is None


def test_plan_draft_for_swing_adds_sell_target_and_unchecked_stop(session):
    idea = ideas.apply_ideas(session, [{**BRK, "horizon": "swing", "entry_low": 480, "entry_high": 500, "stop": 455, "target": 560}], "weekly")[0]
    items = ideas.plan_draft(session, idea)["items"]
    assert [(i["side"], i["price_low"], i["price_high"], i.get("on", True)) for i in items] == [
        ("buy", 480.0, 500.0, True), ("sell", 560.0, None, True), ("sell", None, 455.0, False)]
