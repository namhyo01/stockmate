"""AI 추천 종목: AI가 새로 추천한 티커를 이유·리스크·겹침·출처와 함께 쌓아 둔다 (추천 탭).

AI 응답 끝의 ```ideas 블록(JSON 배열)이나 분석 JSON의 ideas로 들어온다.
    [{"name": "버크셔 해서웨이 B", "symbol": "BRK.B", "account": "토스", "summary": "한 줄",
      "thesis": ["이유", ...], "risks": ["리스크", ...], "overlap": "겹치는 정도",
      "entry_low": null, "entry_high": 505, "sources": [{"title": "...", "url": "https://..."}]}]
계획 탭(plans.py)이 '무엇을 몇 주 언제'라면, 이쪽은 '왜 이 종목인가'를 남긴다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate.market_data import PriceProvider
from stockmate.models import Holding, Idea, Instrument, Market, PlanItem
from stockmate.plans import normalize_account
from stockmate.portfolio import detect_market, normalize_symbol

ACTIVE, DISMISSED, REPLACED = "active", "dismissed", "replaced"

_IDEAS_BLOCK = re.compile(r"```ideas[ \t]*\n(.*?)```", re.S)

IDEAS_GUIDE = """- name: 종목명, symbol: 국내 6자리 코드 또는 미국 티커 (예: BRK.B), account: 담을 계좌 (토스 / 한투 / 삼성연금)
- summary: 한 줄 요약, thesis: 추천 이유 목록, risks: 리스크 목록, overlap: 지금 가진 종목과 겹치는 정도 (한두 문장)
- entry_low, entry_high: 사기 좋은 가격 범위 (종목 통화, 한쪽만 써도 됨, 없으면 null)
- horizon: "swing"(몇 주~몇 달) 또는 "long"(1년 이상), target: 목표가, stop: 종가로 깨면 추천을 철회할 가격 (모르면 null)
- sources: 근거로 쓴 웹 페이지 [{"title": "...", "url": "https://..."}] (답변의 출처와 같게)"""


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, list):
        lines = [" ".join(str(v).split()) for v in value if str(v).strip()]
        text = "\n".join(lines)
    else:
        text = str(value or "").strip()
    return text[:limit] or None


def _sources(value: Any) -> str | None:
    items = []
    for s in value if isinstance(value, list) else []:
        if isinstance(s, dict) and str(s.get("url", "")).startswith(("https://", "http://")):
            items.append({"title": str(s.get("title") or s["url"])[:200], "url": str(s["url"])[:500]})
    return json.dumps(items[:15], ensure_ascii=False) if items else None


def split_ideas_block(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """답변 끝의 ```ideas 블록을 떼어낸다. 없거나 JSON이 깨졌으면 None."""
    match = _IDEAS_BLOCK.search(text)
    if not match:
        return text, None
    stripped = (text[: match.start()] + text[match.end() :]).strip()
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return stripped, None
    if isinstance(data, dict):
        data = data.get("ideas")
    return stripped, data if isinstance(data, list) else None


def apply_ideas(
    session: Session, data: list[dict[str, Any]] | None, source: str, now: datetime | None = None
) -> list[Idea]:
    """추천을 쌓는다. 같은 종목의 예전 추천은 새 추천으로 대체한다."""
    added: list[Idea] = []
    now = (now or datetime.now()).replace(microsecond=0)
    for raw in data or []:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"), 100)
        if not name:
            continue
        symbol = _text(raw.get("symbol"), 20)
        symbol = normalize_symbol(symbol) if symbol else None
        idea = Idea(
            created_at=now,
            updated_at=now,
            source=source,
            name=name,
            symbol=symbol,
            market=detect_market(symbol) if symbol else None,
            account=normalize_account(raw.get("account")),
            summary=_text(raw.get("summary"), 300),
            thesis=_text(raw.get("thesis"), 3000),
            risks=_text(raw.get("risks"), 2000),
            overlap=_text(raw.get("overlap"), 1000),
            entry_low=_decimal(raw.get("entry_low")),
            entry_high=_decimal(raw.get("entry_high")),
            sources=_sources(raw.get("sources")),
            horizon=raw.get("horizon") if raw.get("horizon") in ("swing", "long") else None,
            patterns=_text(",".join(p for p in raw.get("patterns") or [] if isinstance(p, str)), 200)
            if isinstance(raw.get("patterns"), list)
            else None,
            target=_decimal(raw.get("target")),
            stop=_decimal(raw.get("stop")),
            status=ACTIVE,
        )
        for old in active_ideas(session):
            if (symbol and old.symbol == symbol) or (not symbol and old.name == name):
                old.status, old.updated_at = REPLACED, now
        session.add(idea)
        session.flush()
        added.append(idea)
    return added


def active_ideas(session: Session) -> list[Idea]:
    return list(
        session.scalars(select(Idea).where(Idea.status == ACTIVE).order_by(Idea.created_at.desc(), Idea.id.desc()))
    )


def set_status(session: Session, idea_id: int, status: str) -> Idea | None:
    idea = session.get(Idea, idea_id)
    if idea is None or status not in (ACTIVE, DISMISSED):
        return None
    idea.status, idea.updated_at = status, datetime.now().replace(microsecond=0)
    session.flush()
    return idea


def context_text(session: Session, limit: int = 10) -> str:
    """프롬프트용: 이미 추천한 종목 (같은 추천을 반복하지 않고 이어서 판단하도록)."""
    items = active_ideas(session)[:limit]
    if not items:
        return "- 아직 없음"
    return "\n".join(
        f"- {i.created_at:%m-%d} {i.name}({i.symbol or '코드 미정'}, {i.account or '계좌 미정'}"
        f"{', ' + {'swing': '스윙', 'long': '장기'}[i.horizon] if i.horizon else ''}): {i.summary or ''}"
        for i in items
    )


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def idea_rows(session: Session, provider: PriceProvider) -> dict[str, Any]:
    """화면용. 추천 당시 가격이 비어 있으면 지금 가격을 처음 본 가격으로 채운다."""
    shown = active_ideas(session)
    dismissed = list(
        session.scalars(
            select(Idea).where(Idea.status == DISMISSED).order_by(Idea.updated_at.desc()).limit(20)
        )
    )
    keys = {(i.market or detect_market(i.symbol), i.symbol) for i in shown if i.symbol}
    prices = provider.last_prices(keys) if keys else {}
    held = {
        (market, symbol)
        for market, symbol in session.execute(
            select(Instrument.market, Instrument.symbol).join(Holding, Holding.instrument_id == Instrument.id)
        )
    }

    planned = set(session.scalars(select(PlanItem.symbol).where(PlanItem.status == "open", PlanItem.symbol.is_not(None))))

    def row(idea: Idea) -> dict[str, Any]:
        market: Market | None = idea.market or (detect_market(idea.symbol) if idea.symbol else None)
        price = prices.get((market, idea.symbol)) if idea.symbol else None
        if idea.price_at is None and price is not None and idea.status == ACTIVE:
            idea.price_at = price
        change = (price / idea.price_at - 1) * 100 if price is not None and idea.price_at else None
        in_zone = None
        if price is not None and (idea.entry_low or idea.entry_high):
            in_zone = not (
                (idea.entry_high and price > idea.entry_high) or (idea.entry_low and price < idea.entry_low)
            )
        return {
            "id": idea.id,
            "name": idea.name,
            "symbol": idea.symbol,
            "market": market.value if market else None,
            "currency": market.currency if market else "KRW",
            "account": idea.account,
            "summary": idea.summary,
            "thesis": idea.thesis.split("\n") if idea.thesis else [],
            "risks": idea.risks.split("\n") if idea.risks else [],
            "overlap": idea.overlap,
            "entry_low": _num(idea.entry_low),
            "entry_high": _num(idea.entry_high),
            "horizon": idea.horizon,
            "patterns": idea.patterns.split(",") if idea.patterns else [],
            "target": _num(idea.target),
            "stop": _num(idea.stop),
            "below_stop": bool(price is not None and idea.stop and price < idea.stop),
            "hit_target": bool(price is not None and idea.target and price >= idea.target),
            "price_at": _num(idea.price_at),
            "price_now": _num(price),
            "change_pct": _num(change),
            "in_zone": in_zone,
            "held": (market, idea.symbol) in held,
            "planned": idea.symbol in planned,
            "sources": json.loads(idea.sources) if idea.sources else [],
            "source": idea.source,
            "status": idea.status,
            "created_at": idea.created_at.isoformat(),
        }

    result = {"active": [row(i) for i in shown], "dismissed": [row(i) for i in dismissed]}
    session.flush()
    return result


def plan_draft(session: Session, idea: Idea) -> dict[str, Any]:
    """추천 카드 → 계획 탭에 담기 창 초안 (AI 없이 카드 값으로). 이 종목의 열린 계획도 함께 (같은 방향이면 바뀐다)."""
    from stockmate import plans

    market = idea.market or (detect_market(idea.symbol) if idea.symbol else None)
    conditions = [f"철회 기준 {_plain(idea.stop)}" if idea.stop else "", f"목표가 {_plain(idea.target)}" if idea.target else ""]
    item = {
        "side": "buy",
        "account": idea.account,
        "quantity": None,
        "amount_krw": None,
        "price_low": _num(idea.entry_low),
        "price_high": _num(idea.entry_high),
        "timing": " · ".join(c for c in ["추천 가격대에 들어오면", *conditions] if c),
        "reason": idea.summary,
        "due_from": None,
        "due_to": None,
    }
    open_items = [plans.item_dict(i) for i in plans.open_items(session) if idea.symbol and i.symbol == idea.symbol]
    return {"name": idea.name, "market": market.value if market else None, "symbol": idea.symbol,
            "currency": market.currency if market else "KRW", "items": [item], "open": open_items}  # fmt: skip


def _plain(value: Decimal) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
