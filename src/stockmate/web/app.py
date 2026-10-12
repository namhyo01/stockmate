"""웹 대시보드 서버. 실행: `uv run stock web`"""

from __future__ import annotations

import base64
import binascii
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from stockmate import analysis, discovery, ideas, importers, memory, plans, portfolio, screenshots, sync, technical
from stockmate.brokers.kis import KisError
from stockmate.brokers.toss import TossError
from stockmate.config import settings
from stockmate.db import session_scope
from stockmate.market_data import CachedPrices, MarketDataError
from stockmate.models import Account, Market
from stockmate.portfolio import PortfolioError
from stockmate.report import build_snapshot
from stockmate.web.snapshots import SnapshotStore

STATIC_DIR = Path(__file__).parent / "static"

_basic = HTTPBasic(auto_error=False)


def require_login(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """WEB_PASSWORD가 설정돼 있으면 HTTP Basic 인증을 요구한다 (사용자 이름은 무시)."""
    if not settings.web_password:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode(), settings.web_password.encode()
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="stockmate"'})


prices = CachedPrices(sync.price_provider, ttl=timedelta(seconds=15))


def _build() -> dict[str, Any]:
    with session_scope() as session:
        return build_snapshot(session, prices)


# 20초 넘은 스냅샷은 돌려준 뒤 뒤에서 새로 만든다.
snapshots = SnapshotStore(_build, max_age=timedelta(seconds=20))
runner = analysis.AnalysisRunner()
weekly = discovery.WeeklyRunner(lambda: snapshots.get())
# 서버 메모리가 작으므로 질문에 답하거나 스크린샷을 읽는 Claude도 한 번에 하나만 띄운다.
_ask_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 누가 보고 있으면 20초마다, 5분 넘게 아무도 안 보면 10분마다 시세를 미리 받아 둔다.
    # (한투 시세 29종목 ≈ 3초, 초당 20건 제한 안쪽)
    snapshots.keep_warm(active=timedelta(minutes=5), busy_every=20, idle_every=600)
    weekly.schedule()  # 매주 토요일 오전 8시 종목 발굴
    yield


app = FastAPI(
    title="stockmate",
    dependencies=[Depends(require_login)],
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


def page_version() -> str:
    """화면 파일의 버전 (수정 시각). 열려 있는 페이지가 새 버전을 알아채고 스스로 새로고침하는 데 쓴다."""
    return str(int((STATIC_DIR / "index.html").stat().st_mtime))


@app.middleware("http")
async def add_page_version(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-Page-Version"] = page_version()
    return response


def _snapshot(fresh: bool = False) -> dict[str, Any]:
    try:
        return snapshots.build() if fresh else snapshots.get()
    except MarketDataError as e:
        raise HTTPException(503, str(e)) from None


@app.get("/")
def index() -> FileResponse:
    # 배포하면 바로 새 화면이 보이도록 브라우저가 매번 서버에 확인하게 한다 (바뀌지 않았으면 304)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/portfolio")
def get_portfolio() -> dict[str, Any]:
    return _snapshot()


@app.post("/api/refresh")
def refresh() -> dict[str, Any]:
    """API로 연결된 계좌를 동기화하고 시세·환율을 새로 가져온다."""
    messages: list[dict[str, str]] = []
    syncers = [
        ("한국투자증권", sync.kis_configured, sync.sync_kis),
        ("토스증권", sync.toss_configured, sync.sync_toss),
    ]
    for label, configured, run in syncers:
        if not configured():
            continue
        try:
            with session_scope() as session:
                result = run(session)
                messages.append(
                    {
                        "level": "success",
                        "text": f"{result.account.name}: 잔고 {result.domestic + result.overseas}종목 동기화",
                    }
                )
                messages += [{"level": "warning", "text": w} for w in result.warnings]
        except (KisError, TossError, PortfolioError) as e:
            messages.append({"level": "error", "text": f"{label} 동기화 실패: {e}"})
    prices.invalidate()
    return {"messages": messages, "snapshot": _snapshot(fresh=True)}


def _analysis_state() -> dict[str, Any]:
    latest = analysis.latest_analysis()
    return {
        "status": runner.status,
        "started_at": runner.started_at.isoformat(timespec="seconds") if runner.started_at else None,
        "error": runner.error,
        "latest": (
            {"created_at": latest.created_at.isoformat(), "markdown": latest.markdown}
            if latest
            else None
        ),
    }


@app.get("/api/analysis")
def get_analysis() -> dict[str, Any]:
    return _analysis_state()


@app.post("/api/analysis")
def start_analysis() -> dict[str, Any]:
    if not runner.start(_snapshot()):
        raise HTTPException(409, "이미 분석 중이에요.")
    return _analysis_state()


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.post("/api/ask")
def ask(body: Question) -> dict[str, Any]:
    if not _ask_lock.acquire(blocking=False):
        raise HTTPException(409, "AI가 다른 질문이나 스크린샷을 처리하는 중이에요. 잠시 후 다시 시도하세요.")
    try:
        answer = analysis.ask(_snapshot(), body.question)
        return {
            "answer": answer.markdown,
            "profile_notes": answer.profile_notes,
            "plan_changes": answer.plan_changes,
        }
    except analysis.AnalysisError as e:
        raise HTTPException(502, str(e)) from None
    finally:
        _ask_lock.release()


@app.get("/api/memory")
def get_memory() -> dict[str, Any]:
    """Claude가 기억하는 것: 투자자 프로필, 지난 추천(이후 가격 변화 포함). 대화 기록은 /api/conversations."""
    snapshot = _snapshot()
    with session_scope() as session:
        return {
            "profile": memory.read_profile(),
            "recommendations": memory.recommendation_rows(session, snapshot),
        }


@app.get("/api/conversations")
def get_conversations(limit: int = 5) -> dict[str, Any]:
    """지난 질문과 답변, 최신순. 다른 기기에서 물어본 것도 보이도록 파일 기록에서 읽는다."""
    limit = max(1, min(limit, 50))
    entries = memory.recent_conversations(limit + 1)  # 하나 더 읽어서 더 있는지 안다
    return {
        "items": [
            {"created_at": e.created_at.isoformat(), "question": e.question, "answer": e.answer}
            for e in reversed(entries[-limit:])
        ],
        "more": len(entries) > limit,
    }


# ── 매매 계획 ──────────────────────────────────────


def _plan_rows() -> dict[str, Any]:
    try:
        with session_scope() as session:
            return plans.plan_rows(session, prices)
    except MarketDataError as e:
        raise HTTPException(503, str(e)) from None


@app.get("/api/plans")
def get_plans() -> dict[str, Any]:
    return _plan_rows()


class PlanStatus(BaseModel):
    status: str = Field(pattern="^(open|done|canceled)$")


@app.post("/api/plans/{item_id:int}")
def update_plan(item_id: int, body: PlanStatus) -> dict[str, Any]:
    """화면의 '했어요' / '취소' / '되돌리기'."""
    with session_scope() as session:
        if plans.set_status(session, item_id, body.status) is None:
            raise HTTPException(404, "계획을 찾을 수 없어요.")
    snapshots.refresh_in_background()  # 포트폴리오 탭의 계획 표시도 바로 맞춘다
    return _plan_rows()


@app.post("/api/plans/rebuild")
def rebuild_plans() -> dict[str, Any]:
    """Claude가 최근 대화와 리포트를 다시 읽고 계획 목록을 정리한다 (1~2분)."""
    if not _ask_lock.acquire(blocking=False):
        raise HTTPException(409, "AI가 다른 질문이나 스크린샷을 처리하는 중이에요. 잠시 후 다시 시도하세요.")
    try:
        changes = analysis.rebuild_plans(_snapshot())
    except analysis.AnalysisError as e:
        raise HTTPException(502, str(e)) from None
    finally:
        _ask_lock.release()
    snapshots.refresh_in_background()
    return {"changes": changes, **_plan_rows()}


# ── AI 추천 종목 ───────────────────────────────────


@app.get("/api/ideas")
def get_ideas() -> dict[str, Any]:
    try:
        with session_scope() as session:
            return ideas.idea_rows(session, prices)
    except MarketDataError as e:
        raise HTTPException(503, str(e)) from None


class IdeaStatus(BaseModel):
    status: str = Field(pattern="^(active|dismissed)$")


@app.post("/api/ideas/{idea_id:int}")
def update_idea(idea_id: int, body: IdeaStatus) -> dict[str, Any]:
    """화면의 '관심 없음' / '다시 보기'."""
    with session_scope() as session:
        if ideas.set_status(session, idea_id, body.status) is None:
            raise HTTPException(404, "추천을 찾을 수 없어요.")
    return get_ideas()


# ── 차트 ───────────────────────────────────────────


@app.get("/api/chart")
def get_chart(market: Market, symbol: str = Query(min_length=1, max_length=20), name: str | None = None) -> dict[str, Any]:
    """종목의 일봉, 이동평균·일목균형표·볼린저, AI에게 넘기는 차트 요약."""
    payload = technical.chart_payload(market, portfolio.normalize_symbol(symbol), name)
    if payload is None:
        raise HTTPException(404, "일봉을 가져오지 못했어요.")
    return payload


# ── 매주 종목 발굴 ─────────────────────────────────


def _discovery_state() -> dict[str, Any]:
    last = discovery.latest_run()
    return {
        "status": weekly.status,
        "started_at": weekly.started_at.isoformat(timespec="seconds") if weekly.started_at else None,
        "error": weekly.error,
        "last": last,
    }


@app.get("/api/discovery")
def get_discovery() -> dict[str, Any]:
    return _discovery_state()


@app.post("/api/discovery")
def start_discovery() -> dict[str, Any]:
    """지금 바로 종목 발굴을 돌린다 (5~10분). 정기 실행은 토요일 오전 8시."""
    if not weekly.start():
        raise HTTPException(409, "이미 종목을 찾는 중이에요.")
    return _discovery_state()


@app.get("/api/ideas/scorecard")
def get_scorecard() -> dict[str, Any]:
    with session_scope() as session:
        return discovery.scorecard(session)


class Profile(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.put("/api/memory/profile")
def put_profile(body: Profile) -> dict[str, str]:
    memory.write_profile(body.text)
    return {"profile": memory.read_profile()}


# ── 스크린샷으로 잔고 갱신 (토스·삼성) ─────────────────


def _screenshot_account(session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "계좌를 찾을 수 없어요.")
    if sync.api_synced(account):
        raise HTTPException(400, f"{account.name}은 증권사 API로 자동 동기화돼요.")
    return account


class Screenshot(BaseModel):
    media_type: str
    data: str = Field(max_length=16_000_000)  # base64


class ScreenshotUpload(BaseModel):
    account_id: int
    images: list[Screenshot] = Field(min_length=1, max_length=screenshots.MAX_IMAGES)


@app.post("/api/screenshots/read")
def read_screenshots(body: ScreenshotUpload) -> dict[str, Any]:
    """스크린샷을 Claude가 읽고, 평단 역산·티커 검증·기존 잔고 비교까지 한 미리보기를 돌려준다.

    DB는 바꾸지 않는다. 사용자가 확인하고 /api/screenshots/apply를 불러야 반영된다.
    """
    try:
        images = [screenshots.Image(base64.b64decode(i.data, validate=True), i.media_type) for i in body.images]
    except binascii.Error:
        raise HTTPException(400, "이미지 데이터가 올바르지 않아요.") from None
    if not _ask_lock.acquire(blocking=False):
        raise HTTPException(409, "AI가 다른 질문이나 스크린샷을 처리하는 중이에요. 잠시 후 다시 시도하세요.")
    try:
        with session_scope() as session:
            account = _screenshot_account(session, body.account_id)
            extracted = screenshots.read_screenshots(session, account, images)
            draft = screenshots.build_draft(account, extracted, prices)
            return {"account": {"id": account.id, "name": account.name}, **screenshots.draft_to_dict(draft)}
    except screenshots.ScreenshotError as e:
        raise HTTPException(502, str(e)) from None
    except MarketDataError as e:
        raise HTTPException(503, str(e)) from None
    finally:
        _ask_lock.release()


class ApplyRow(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    market: Market
    name: str | None = Field(default=None, max_length=100)
    quantity: str
    avg_cost: str


class RemoveRow(BaseModel):
    symbol: str
    market: Market


class ScreenshotApply(BaseModel):
    account_id: int
    rows: list[ApplyRow] = Field(max_length=200)
    remove: list[RemoveRow] = Field(default_factory=list, max_length=200)
    cash_krw: str | None = None


def _number(text: str, label: str, symbol: str, allow_zero: bool = False) -> Decimal:
    try:
        value = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        raise HTTPException(400, f"{symbol}의 {label}를 숫자로 읽을 수 없어요.") from None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise HTTPException(400, f"{symbol}의 {label}는 0보다 커야 해요.")
    return value


@app.post("/api/screenshots/apply")
def apply_screenshots(body: ScreenshotApply) -> dict[str, Any]:
    """미리보기에서 체크한 종목만 덮어쓰고, 지우기로 한 종목만 지운다."""
    rows = [
        importers.HoldingRow(
            r.symbol.strip().upper(),
            _number(r.quantity, "수량", r.symbol),
            _number(r.avg_cost, "평균단가", r.symbol),
            (r.name or "").strip() or None,
            r.market,
        )
        for r in body.rows
    ]
    cash = _number(body.cash_krw, "예수금", "계좌", allow_zero=True) if body.cash_krw else None
    try:
        with session_scope() as session:
            account = _screenshot_account(session, body.account_id)
            result = screenshots.apply_rows(
                session, account, rows, [(r.market, r.symbol.strip().upper()) for r in body.remove], cash
            )
            name = account.name
    except portfolio.PortfolioError as e:
        raise HTTPException(400, str(e)) from None
    parts = [f"{result.updated}종목 반영"]
    if result.removed:
        parts.append(f"{len(result.removed)}종목 삭제")
    return {"message": f"{name}: {', '.join(parts)}", "snapshot": _snapshot(fresh=True)}
