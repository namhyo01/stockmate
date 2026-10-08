from datetime import datetime
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from stockmate import memory, plans
from stockmate.db import make_engine
from stockmate.models import Market, Side
from test_portfolio import FakePrices


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


SELL_HYNIX = {
    "side": "sell", "name": "SK하이닉스", "symbol": "000660", "account": "토스", "quantity": 1,
    "amount_krw": None, "price_low": "1,900,000", "price_high": None, "timing": "10월 하순 실적 후", "reason": "비중 축소",
}  # fmt: skip
BUY_SNP = {"side": "buy", "name": "TIGER 미국S&P500", "symbol": "360750", "account": "한투", "quantity": 12, "price_high": 26000}


def test_split_actions_block_removes_it_from_answer():
    answer = '결론이에요.\n\n```actions\n{"add": [], "done": [3]}\n```\n'
    text, data = plans.split_actions_block(answer)
    assert text == "결론이에요." and data == {"add": [], "done": [3]}
    assert plans.split_actions_block("```actions\n{깨짐\n```") == ("", None)
    assert plans.split_actions_block("블록 없음") == ("블록 없음", None)


def test_apply_add_done_cancel_and_ignores_bad_rows(session):
    result = plans.apply_actions(session, {"add": [SELL_HYNIX, BUY_SNP, {"side": "hold", "name": "x"}, {"side": "buy"}]}, "chat")
    hynix, snp = result.added
    assert len(result.added) == 2 and result.summary == ["계획 2건 추가"]
    assert hynix.side is Side.SELL and hynix.price_low == D(1900000) and hynix.market is Market.KRX
    assert snp.quantity == D(12) and snp.price_high == D(26000)

    result = plans.apply_actions(session, {"done": [hynix.id], "cancel": [snp.id, 999, "x"]}, "chat")
    assert result.summary == ["1건 완료", "1건 취소"]
    assert hynix.status == "done" and snp.status == "canceled" and hynix.closed_at is not None
    assert plans.open_items(session) == []


def test_same_stock_and_side_replaces_old_plan(session):
    old = plans.apply_actions(session, {"add": [SELL_HYNIX]}, "analysis", now=datetime(2026, 10, 1)).added[0]
    newer = {**SELL_HYNIX, "price_low": 2000000}
    new = plans.apply_actions(session, {"add": [newer, {**BUY_SNP, "account": "삼성연금"}]}, "chat").added[0]
    assert old.status == "replaced"
    assert [i.id for i in plans.open_items(session)] == [new.id, new.id + 1]


def test_condition_state_and_gap(session):
    sell, buy = plans.apply_actions(session, {"add": [SELL_HYNIX, BUY_SNP]}, "chat").added
    state, gap = plans.condition_state(sell, D(1800000))
    assert state == "wait" and round(gap, 2) == D("5.56")  # 5.56% 더 올라야 한다
    assert plans.condition_state(sell, D(1950000)) == ("met", None)
    state, gap = plans.condition_state(buy, D(27000))
    assert state == "wait" and gap < 0
    assert plans.condition_state(buy, None) == ("none", None)


def test_plan_rows_include_current_price(session):
    plans.apply_actions(session, {"add": [SELL_HYNIX, {"side": "buy", "name": "코드 모르는 ETF"}]}, "chat")
    rows = plans.plan_rows(session, FakePrices({(Market.KRX, "000660"): D(1950000)}))
    hynix, etf = rows["open"]
    assert hynix["price_now"] == 1950000 and hynix["condition"] == "met"
    assert etf["price_now"] is None and etf["condition"] == "none"
    assert rows["closed"] == []


def test_open_plans_go_into_prompt_context(session):
    plans.apply_actions(session, {"add": [SELL_HYNIX]}, "chat")
    text = plans.context_text(session)
    assert text.startswith("- #1 [매도] SK하이닉스(000660, 토스)") and "1,900,000 이상" in text


def test_parse_report_reads_actions():
    report = memory.parse_report('본문\n```json\n{"recommendations": [], "lessons": [], "actions": {"add": [], "done": [1]}}\n```')
    assert report.actions == {"add": [], "done": [1]}
    assert memory.parse_report('본문\n```json\n{"recommendations": []}\n```').actions is None


@pytest.mark.parametrize(
    ("text", "account"),
    [("토스증권", "토스"), ("ISA", "한투"), ("한투 ISA", "한투"), ("연금", "삼성연금"), ("연금저축", "삼성연금"), (None, None), ("키움", "키움")],
)
def test_normalize_account(text, account):
    assert plans.normalize_account(text) == account


def test_plan_account_is_normalized(session):
    [item] = plans.apply_actions(session, {"add": [{**BUY_SNP, "account": "연금저축"}]}, "chat").added
    assert item.account == "삼성연금"


def test_due_dates_are_parsed_and_shown(session):
    [item, no_date] = plans.apply_actions(
        session,
        {"add": [{**SELL_HYNIX, "due_from": "2026-10-27", "due_to": None}, {**BUY_SNP, "due_from": "모름"}]},
        "chat",
    ).added
    assert (item.due_from, item.due_to) == (datetime(2026, 10, 27).date(), datetime(2026, 10, 27).date())
    assert no_date.due_from is None and no_date.due_to is None
    row = plans.item_dict(item)
    assert row["due_from"] == "2026-10-27" and "2026-10-27~2026-10-27" in plans.describe(item)


def test_tranches_in_one_batch_coexist_but_replace_older_plan(session):
    [old] = plans.apply_actions(session, {"add": [BUY_SNP]}, "chat", now=datetime(2026, 10, 1)).added
    first, second = plans.apply_actions(
        session,
        {"add": [{**BUY_SNP, "quantity": 7, "due_from": "2026-10-06"}, {**BUY_SNP, "quantity": 8, "due_from": "2026-10-29"}]},
        "chat",
    ).added
    assert old.status == "replaced"
    assert [i.id for i in plans.open_items(session)] == [first.id, second.id]
