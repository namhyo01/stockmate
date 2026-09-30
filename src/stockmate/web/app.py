"""웹 대시보드 서버. 실행: `uv run stock web`"""

from __future__ import annotations

import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from stockmate import analysis, memory, sync
from stockmate.brokers.kis import KisError
from stockmate.config import settings
from stockmate.db import session_scope
from stockmate.market_data import CachedPrices, MarketDataError
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


prices = CachedPrices(sync.price_provider, ttl=timedelta(seconds=30))


def _build() -> dict[str, Any]:
    with session_scope() as session:
        return build_snapshot(session, prices)


# 1분 넘은 스냅샷은 돌려준 뒤 뒤에서 새로 만든다.
snapshots = SnapshotStore(_build, max_age=timedelta(minutes=1))
runner = analysis.AnalysisRunner()
# 서버 메모리가 작으므로 질문에 답하는 Claude도 한 번에 하나만 띄운다.
_ask_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 누가 보고 있으면 1분마다, 15분 넘게 아무도 안 보면 10분마다 시세를 미리 받아 둔다.
    snapshots.keep_warm(active=timedelta(minutes=15), busy_every=60, idle_every=600)
    yield


app = FastAPI(
    title="stockmate",
    dependencies=[Depends(require_login)],
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


def _snapshot(fresh: bool = False) -> dict[str, Any]:
    try:
        return snapshots.build() if fresh else snapshots.get()
    except MarketDataError as e:
        raise HTTPException(503, str(e)) from None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/portfolio")
def get_portfolio() -> dict[str, Any]:
    return _snapshot()


@app.post("/api/refresh")
def refresh() -> dict[str, Any]:
    """API로 연결된 계좌를 동기화하고 시세·환율을 새로 가져온다."""
    messages: list[dict[str, str]] = []
    if sync.kis_configured():
        try:
            with session_scope() as session:
                result = sync.sync_kis(session)
                messages.append(
                    {
                        "level": "success",
                        "text": f"{result.account.name}: 잔고 {result.domestic + result.overseas}종목 동기화",
                    }
                )
                messages += [{"level": "warning", "text": w} for w in result.warnings]
        except (KisError, PortfolioError) as e:
            messages.append({"level": "error", "text": f"한국투자증권 동기화 실패: {e}"})
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
        raise HTTPException(409, "다른 질문에 답하는 중이에요. 잠시 후 다시 시도하세요.")
    try:
        answer = analysis.ask(_snapshot(), body.question)
        return {"answer": answer.markdown, "profile_notes": answer.profile_notes}
    except analysis.AnalysisError as e:
        raise HTTPException(502, str(e)) from None
    finally:
        _ask_lock.release()


@app.get("/api/memory")
def get_memory() -> dict[str, Any]:
    """Claude가 기억하는 것: 투자자 프로필, 지난 추천(이후 가격 변화 포함).

    대화 기록은 data/memory/conversations/에 파일로만 남기고 화면에는 보여주지 않는다.
    """
    snapshot = _snapshot()
    with session_scope() as session:
        return {
            "profile": memory.read_profile(),
            "recommendations": memory.recommendation_rows(session, snapshot),
        }


class Profile(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.put("/api/memory/profile")
def put_profile(body: Profile) -> dict[str, str]:
    memory.write_profile(body.text)
    return {"profile": memory.read_profile()}
