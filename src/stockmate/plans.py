"""매매 계획: Claude와 정한 '무엇을, 얼마나, 어떤 가격·시기에 사고팔지'를 구조화해서 쌓는다.

Claude 응답 끝의 actions 블록(JSON)으로 추가·완료·취소된다.
    {"add": [{"side": "buy", "name": "TIGER 미국S&P500", "symbol": "360750", "account": "한투",
              "quantity": 12, "amount_krw": null, "price_low": null, "price_high": 26000,
              "timing": "FOMC 결과 뒤", "reason": "..."}],
     "done": [3], "cancel": [5]}
사용자는 대시보드 '계획' 탭에서 '했어요'/'취소'로 직접 닫을 수도 있다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate.market_data import PriceProvider
from stockmate.models import PlanItem, Side
from stockmate.portfolio import detect_market, normalize_symbol

OPEN, DONE, CANCELED, REPLACED = "open", "done", "canceled", "replaced"
CLOSED_STATUSES = (DONE, CANCELED, REPLACED)

_ACTIONS_BLOCK = re.compile(r"```actions[ \t]*\n(.*?)```", re.S)

# 프롬프트에 넣는 설명. 대화·분석·정리 프롬프트가 같이 쓴다.
ACTIONS_GUIDE = """- side: "buy" 또는 "sell"
- name: 종목명 (필수), symbol: 국내 6자리 코드 또는 미국 티커 (모르면 null)
- account: 반드시 "토스" / "삼성연금" / "한투" 중 하나 (토스=일반 계좌, 삼성연금=연금저축, 한투=ISA). 화면에서 계좌별로 나눠 보여준다.
  계좌 규칙에 맞게 고르세요: 연금은 ETF·펀드만(레버리지 불가), ISA는 국내 상장 상품만, 개별주와 해외 직접 투자는 토스
- quantity: 주 수, amount_krw: 원화 금액. 아는 것만 채우고 나머지는 null
- price_low, price_high: 이 가격 범위에 들어오면 실행 (종목 통화 기준, 한쪽만 써도 됨. 예: 190만원 이상이면 매도 → price_low 1900000)
- timing: 시기나 조건을 짧게 (예: "10월 하순 실적 발표 후"), reason: 한 줄 근거
- due_from, due_to: 실행할 날짜 범위 "YYYY-MM-DD" (예: 다음 주면 그 주 월요일~금요일, 실적 발표 후면 발표 예정일~그 주 금요일, 지금 바로면 오늘~이번 주 금요일).
  날짜와 상관없이 가격·사건 조건만 있는 계획이면 둘 다 null. 화면에서 이 날짜로 '이번 주 / 다음 주 / 이번 달' 같이 묶는다
- 나눠서 사고파는 계획(예: 7주는 이번 주, 8주는 FOMC 뒤)은 회차마다 따로 add하세요. 회차마다 quantity, 가격, due_from~due_to가 달라야 화면에서 시기별로 나뉜다.
- 이미 있는 계획과 같은 종목·같은 방향을 add하면 예전 계획(모든 회차)을 대신한다. 그러니 계획을 고칠 때는 남은 회차를 전부 다시 넣는다.
  실행했거나 그만두기로 한 계획은 번호를 done / cancel에 넣는다."""


@dataclass
class ActionResult:
    added: list[PlanItem] = field(default_factory=list)
    done: list[PlanItem] = field(default_factory=list)
    canceled: list[PlanItem] = field(default_factory=list)

    @property
    def summary(self) -> list[str]:
        parts = []
        if self.added:
            parts.append(f"계획 {len(self.added)}건 추가")
        if self.done:
            parts.append(f"{len(self.done)}건 완료")
        if self.canceled:
            parts.append(f"{len(self.canceled)}건 취소")
        return parts


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


# 계좌 이름을 DB 이름으로 맞춘다 (AI가 'ISA', '연금'처럼 써도 된다)
ACCOUNTS = ("토스", "삼성연금", "한투")
_ACCOUNT_ALIASES = {
    "토스": ("토스", "toss"),
    "삼성연금": ("삼성", "연금", "pension", "samsung"),
    "한투": ("한투", "한국투자", "isa", "kis"),
}


def normalize_account(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    for name, aliases in _ACCOUNT_ALIASES.items():
        if any(alias in text for alias in aliases):
            return name
    return str(value).strip()[:50]


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def _text(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def split_actions_block(text: str) -> tuple[str, dict[str, Any] | None]:
    """답변 끝의 ```actions 블록을 떼어낸다. 블록이 없거나 JSON이 깨졌으면 None."""
    match = _ACTIONS_BLOCK.search(text)
    if not match:
        return text, None
    stripped = (text[: match.start()] + text[match.end() :]).strip()
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return stripped, None
    return stripped, data if isinstance(data, dict) else None


def apply_actions(
    session: Session, data: dict[str, Any] | None, source: str, now: datetime | None = None
) -> ActionResult:
    """actions 블록을 반영한다. 같은 종목·같은 방향의 열린 계획은 새 계획으로 대체한다."""
    result = ActionResult()
    if not data:
        return result
    now = (now or datetime.now()).replace(microsecond=0)

    for key, status, bucket in (("done", DONE, result.done), ("cancel", CANCELED, result.canceled)):
        for raw_id in data.get(key) or []:
            try:
                item = session.get(PlanItem, int(raw_id))
            except (TypeError, ValueError):
                continue
            if item is not None and item.status == OPEN:
                _close(item, status, now)
                bucket.append(item)

    for raw in data.get("add") or []:
        if not isinstance(raw, dict):
            continue
        side_text = str(raw.get("side") or "").lower()
        name = _text(raw.get("name"), 100)
        if side_text not in ("buy", "sell") or not name:
            continue
        symbol = _text(raw.get("symbol"), 20)
        symbol = normalize_symbol(symbol) if symbol else None
        account = normalize_account(raw.get("account"))
        item = PlanItem(
            created_at=now,
            updated_at=now,
            source=source,
            side=Side(side_text),
            name=name,
            symbol=symbol,
            market=detect_market(symbol) if symbol else None,
            account=account,
            quantity=_decimal(raw.get("quantity")),
            amount_krw=_decimal(raw.get("amount_krw")),
            price_low=_decimal(raw.get("price_low")),
            price_high=_decimal(raw.get("price_high")),
            timing=_text(raw.get("timing"), 200),
            due_from=parse_date(raw.get("due_from")),
            due_to=parse_date(raw.get("due_to")) or parse_date(raw.get("due_from")),
            # AI가 due_from 칸을 직접 채워 보냈으면 (null이라도) 날짜 판단을 마친 것이다
            due_checked="due_from" in raw,
            reason=_text(raw.get("reason"), 500),
            status=OPEN,
        )
        # 예전 계획만 대신한다. 이번에 같이 들어온 회차(분할매수)끼리는 서로 덮어쓰지 않는다.
        new_ids = {i.id for i in result.added}
        for old in open_items(session):
            if old.id not in new_ids and old.side is item.side and _same_target(old, item):
                _close(old, REPLACED, now)
        session.add(item)
        session.flush()
        result.added.append(item)
    return result


def _same_target(a: PlanItem, b: PlanItem) -> bool:
    same_stock = (a.symbol and a.symbol == b.symbol) or (not (a.symbol and b.symbol) and a.name == b.name)
    return bool(same_stock) and (a.account or None) == (b.account or None)


def _close(item: PlanItem, status: str, now: datetime) -> None:
    item.status, item.closed_at, item.updated_at = status, now, now


def set_status(session: Session, item_id: int, status: str) -> PlanItem | None:
    """사용자가 화면에서 '했어요'/'취소'/'되돌리기'를 눌렀을 때."""
    item = session.get(PlanItem, item_id)
    if item is None or status not in (OPEN, DONE, CANCELED):
        return None
    now = datetime.now().replace(microsecond=0)
    if status == OPEN:
        item.status, item.closed_at, item.updated_at = OPEN, None, now
    else:
        _close(item, status, now)
    session.flush()
    return item


def open_items(session: Session) -> list[PlanItem]:
    return list(
        session.scalars(select(PlanItem).where(PlanItem.status == OPEN).order_by(PlanItem.created_at, PlanItem.id))
    )


def closed_items(session: Session, limit: int = 30) -> list[PlanItem]:
    return list(
        session.scalars(
            select(PlanItem)
            .where(PlanItem.status.in_(CLOSED_STATUSES))
            .order_by(PlanItem.closed_at.desc(), PlanItem.id.desc())
            .limit(limit)
        )
    )


def describe(item: PlanItem) -> str:
    """프롬프트용 한 줄."""
    side = "매수" if item.side is Side.BUY else "매도"
    target = f"{item.name}({item.symbol or '코드 미정'}, {item.account or '계좌 미정'})"
    size = ", ".join(
        x
        for x in (
            f"{item.quantity.normalize():f}주" if item.quantity else "",
            f"{item.amount_krw:,.0f}원" if item.amount_krw else "",
        )
        if x
    )
    price = _price_condition(item)
    due = ""
    if item.due_from or item.due_to:
        due = f"{item.due_from or ''}~{item.due_to or ''}"
    extra = " / ".join(x for x in (size, price, item.timing or "", due, item.reason or "") if x)
    return f"#{item.id} [{side}] {target} {extra} (정한 날 {item.created_at:%m-%d})"


def _price_condition(item: PlanItem) -> str:
    low, high = item.price_low, item.price_high
    fmt = lambda v: f"{v.normalize():,f}"  # noqa: E731
    if low and high:
        return f"{fmt(low)}~{fmt(high)}"
    if low:
        return f"{fmt(low)} 이상"
    if high:
        return f"{fmt(high)} 이하"
    return ""


def context_text(session: Session) -> str:
    items = open_items(session)
    if not items:
        return "- 아직 없음"
    return "\n".join(f"- {describe(i)}" for i in items)


# ── 화면용 ───────────────────────────────────────────


def condition_state(item: PlanItem, price: Decimal | None) -> tuple[str, Decimal | None]:
    """가격 조건과 현재가 비교: (met | wait | none, 조건까지 남은 %).

    남은 %는 현재가가 얼마나 움직여야 조건에 닿는지 (+면 올라야, −면 내려야).
    """
    if price is None or not (item.price_low or item.price_high):
        return "none", None
    if item.price_high and price > item.price_high:
        return "wait", (item.price_high / price - 1) * 100
    if item.price_low and price < item.price_low:
        return "wait", (item.price_low / price - 1) * 100
    return "met", None


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def item_dict(item: PlanItem, price: Decimal | None = None) -> dict[str, Any]:
    state, gap = condition_state(item, price)
    market = item.market or (detect_market(item.symbol) if item.symbol else None)
    return {
        "id": item.id,
        "side": item.side.value,
        "name": item.name,
        "symbol": item.symbol,
        "currency": market.currency if market else "KRW",
        "account": item.account,
        "quantity": _num(item.quantity),
        "amount_krw": _num(item.amount_krw),
        "price_low": _num(item.price_low),
        "price_high": _num(item.price_high),
        "timing": item.timing,
        "due_from": item.due_from.isoformat() if item.due_from else None,
        "due_to": item.due_to.isoformat() if item.due_to else None,
        "reason": item.reason,
        "source": item.source,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "closed_at": item.closed_at.isoformat() if item.closed_at else None,
        "price_now": _num(price),
        "condition": state,
        "gap_pct": _num(gap),
    }


def plan_rows(session: Session, provider: PriceProvider) -> dict[str, Any]:
    opened, closed = open_items(session), closed_items(session)
    keys = {(i.market or detect_market(i.symbol), i.symbol) for i in opened if i.symbol}
    prices = provider.last_prices(keys) if keys else {}

    def price_of(item: PlanItem) -> Decimal | None:
        if not item.symbol:
            return None
        return prices.get((item.market or detect_market(item.symbol), item.symbol))

    return {
        "open": [item_dict(i, price_of(i)) for i in opened],
        "closed": [item_dict(i) for i in closed],
    }

