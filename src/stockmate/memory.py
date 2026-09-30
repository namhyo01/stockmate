"""투자 비서의 기억: 투자자 프로필, 지난 추천, 대화 기록.

Claude는 매번 새 세션으로 실행되므로, 분석이나 질문 때마다 이 기억을 프롬프트에 넣어
하나의 흐름처럼 일관되게 판단하게 한다.

- data/memory/profile.md: 성향, 원칙, 진행 중인 결정과 계획, 교훈 (사람이 직접 고칠 수 있다)
- data/memory/conversations/YYYY-MM.md: 질문과 답변 전체 기록 (프롬프트에는 최근 몇 개만 넣는다)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate.config import settings
from stockmate.models import Recommendation

PROFILE_PATH = settings.db_path.parent / "memory" / "profile.md"

# 프롬프트가 너무 길어지지 않도록 넣을 기억의 양을 제한한다.
RECENT_ANALYSES = 2
RECENT_CONVERSATIONS = 5

DEFAULT_PROFILE = """# 투자자 프로필

## 기본 정보
- 계좌: 토스(일반, 국내·미국 개별주), 한투(ISA 중개형 서민형, 국내 상장 ETF 위주), 삼성(연금저축, 매달 40만원 적립 예정)
- 투자 스타일: 장기·스윙·단기가 섞여 있음
- 매매는 본인이 직접 하고, Claude는 분석과 제안만 한다

## 투자 목표와 원칙
- (아직 정하지 않음: 목표 수익률, 투자 기간, 감당할 수 있는 손실폭)

## 진행 중인 결정과 계획

## 대화에서 알게 된 것

## 지난 추천에서 배운 점
"""

_PROFILE_SECTIONS = {
    "plan": "## 진행 중인 결정과 계획",
    "conversation": "## 대화에서 알게 된 것",
    "lesson": "## 지난 추천에서 배운 점",
}

# Claude 응답 끝에 붙는 기계용 블록. 화면에 보여주기 전에 떼어낸다.
_JSON_BLOCK = re.compile(r"```json\s*(.*?)```\s*$", re.S)
_MEMORY_BLOCK = re.compile(r"```(profile|plan)[ \t]*\n(.*?)```", re.S)
_ENTRY = "<!-- entry -->"


# ── 프로필 ─────────────────────────────────────────


def read_profile() -> str:
    try:
        return PROFILE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_PROFILE


def write_profile(text: str) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(text.strip() + "\n", encoding="utf-8")


def add_profile_notes(kind: str, notes: list[str]) -> list[str]:
    """프로필의 해당 섹션 끝에 날짜를 붙여 메모를 추가한다. 실제로 추가한 메모를 돌려준다."""
    notes = [n.strip().lstrip("-• ").strip() for n in notes if n.strip()]
    if not notes:
        return []
    heading = _PROFILE_SECTIONS[kind]
    profile = read_profile()
    if heading not in profile:
        profile = profile.rstrip() + f"\n\n{heading}\n"
    today = datetime.now().strftime("%Y-%m-%d")
    lines = "".join(f"- ({today}) {n}\n" for n in notes)
    # 해당 섹션의 끝(다음 ## 제목 직전)에 끼워 넣는다.
    start = profile.index(heading) + len(heading)
    next_heading = profile.find("\n## ", start)
    end = len(profile) if next_heading == -1 else next_heading
    section = profile[start:end].rstrip("\n")
    rest = profile[end:].lstrip("\n")
    profile = profile[:start] + section + "\n" + lines + (f"\n{rest}" if rest else "")
    write_profile(profile)
    return notes


# ── 응답 파싱 ───────────────────────────────────────


@dataclass
class ParsedReport:
    markdown: str
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)


def parse_report(text: str) -> ParsedReport:
    """리포트 끝의 ```json 블록(추천 목록, 교훈)을 떼어낸다. 블록이 없거나 깨졌으면 본문만 돌려준다."""
    match = _JSON_BLOCK.search(text)
    if not match:
        return ParsedReport(text.strip())
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ParsedReport(text[: match.start()].strip())
    return ParsedReport(
        text[: match.start()].strip(),
        [r for r in data.get("recommendations", []) if isinstance(r, dict) and r.get("symbol")],
        [str(x) for x in data.get("lessons", []) if str(x).strip()],
    )


def split_memory_blocks(text: str) -> tuple[str, dict[str, list[str]]]:
    """답변에서 기억용 블록을 떼어낸다: ```profile(새로 알게 된 성향), ```plan(결정과 계획)."""
    notes: dict[str, list[str]] = {"profile": [], "plan": []}
    for kind, body in _MEMORY_BLOCK.findall(text):
        notes[kind] += [line for line in body.splitlines() if line.strip()]
    return _MEMORY_BLOCK.sub("", text).strip(), notes


# ── 추천 기록 ───────────────────────────────────────


def _decimal(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def save_recommendations(
    session: Session, analysis_at: datetime, items: list[dict[str, Any]], snapshot: dict[str, Any]
) -> int:
    by_symbol = {p["symbol"]: p for p in snapshot["positions"]}
    for item in items:
        symbol = str(item["symbol"]).strip().upper()
        position = by_symbol.get(symbol, {})
        session.add(
            Recommendation(
                analysis_at=analysis_at,
                symbol=symbol,
                name=item.get("name") or position.get("name"),
                account=item.get("account") or position.get("account"),
                opinion=str(item.get("opinion", ""))[:20],
                reason=(item.get("reason") or "")[:500] or None,
                price=_decimal(position.get("price")),
                currency=position.get("currency"),
                watch_low=_decimal(item.get("watch_low")),
                watch_high=_decimal(item.get("watch_high")),
            )
        )
    return len(items)


def recent_recommendations(session: Session, analyses: int = RECENT_ANALYSES) -> list[Recommendation]:
    times = session.scalars(
        select(Recommendation.analysis_at)
        .distinct()
        .order_by(Recommendation.analysis_at.desc())
        .limit(analyses)
    ).all()
    if not times:
        return []
    return list(
        session.scalars(
            select(Recommendation)
            .where(Recommendation.analysis_at.in_(times))
            .order_by(Recommendation.analysis_at.desc(), Recommendation.id)
        )
    )


def recommendation_rows(session: Session, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """지난 추천과 그 이후 가격 변화 (대시보드와 프롬프트에서 같이 쓴다)."""
    now = {p["symbol"]: p.get("price") for p in snapshot["positions"]}
    rows = []
    for r in recent_recommendations(session):
        current = now.get(r.symbol)
        change = (
            (current / float(r.price) - 1) * 100 if current is not None and r.price else None
        )
        rows.append(
            {
                "analysis_at": r.analysis_at.isoformat(timespec="minutes"),
                "symbol": r.symbol,
                "name": r.name or r.symbol,
                "account": r.account,
                "opinion": r.opinion,
                "reason": r.reason,
                "currency": r.currency,
                "price_then": float(r.price) if r.price is not None else None,
                "price_now": current,
                "change_pct": change,
                "watch_low": float(r.watch_low) if r.watch_low is not None else None,
                "watch_high": float(r.watch_high) if r.watch_high is not None else None,
            }
        )
    return rows


# ── 대화 ──────────────────────────────────────────


@dataclass(frozen=True)
class ConversationEntry:
    created_at: datetime
    question: str
    answer: str


def _conversations_dir():
    return PROFILE_PATH.parent / "conversations"


def save_conversation(question: str, answer: str, at: datetime | None = None) -> None:
    """월별 마크다운 파일에 질문과 답변을 덧붙인다."""
    at = (at or datetime.now()).replace(microsecond=0)
    folder = _conversations_dir()
    folder.mkdir(parents=True, exist_ok=True)
    question = " ".join(question.split())
    with open(folder / f"{at:%Y-%m}.md", "a", encoding="utf-8") as f:
        f.write(f"{_ENTRY}\n## {at:%Y-%m-%d %H:%M:%S}\n\n**Q.** {question}\n\n{answer.strip()}\n\n")


def _parse_entries(text: str) -> list[ConversationEntry]:
    entries = []
    for chunk in text.split(_ENTRY)[1:]:
        header, _, rest = chunk.strip().partition("\n")
        question, _, answer = rest.strip().partition("\n\n")
        try:
            created_at = datetime.strptime(header.removeprefix("## ").strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        entries.append(ConversationEntry(created_at, question.removeprefix("**Q.**").strip(), answer.strip()))
    return entries


def recent_conversations(limit: int = RECENT_CONVERSATIONS) -> list[ConversationEntry]:
    """최근 대화를 오래된 것부터 돌려준다."""
    entries: list[ConversationEntry] = []
    for path in sorted(_conversations_dir().glob("*.md"), reverse=True):
        entries = _parse_entries(path.read_text(encoding="utf-8")) + entries
        if len(entries) >= limit:
            break
    return entries[-limit:]


# ── 프롬프트용 기억 ──────────────────────────────────


def build_context(session: Session, snapshot: dict[str, Any]) -> str:
    parts = ["### 투자자 프로필", read_profile().strip()]

    recs = recommendation_rows(session, snapshot)
    parts.append("### 지난 추천과 이후 가격 변화")
    if recs:
        for r in recs:
            change = f"{r['change_pct']:+.1f}%" if r["change_pct"] is not None else "현재가 없음"
            parts.append(
                f"- {r['analysis_at'][:10]} {r['name']}({r['symbol']}, {r['account']}): "
                f"{r['opinion']} / 당시 {r['price_then']} → 지금 {r['price_now']} ({change}) / 근거: {r['reason']}"
            )
    else:
        parts.append("- 아직 없음")

    convs = recent_conversations()
    parts.append("### 최근 대화")
    if convs:
        for c in convs:
            answer = c.answer if len(c.answer) <= 600 else c.answer[:600] + "…"
            parts.append(f"- [{c.created_at:%m-%d %H:%M}] Q: {c.question}\n  A: {answer}")
    else:
        parts.append("- 아직 없음")
    return "\n".join(parts)
