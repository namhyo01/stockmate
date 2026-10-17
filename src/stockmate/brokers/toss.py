"""토스증권 Open API 클라이언트 (조회 전용).

문서: https://developers.tossinvest.com (원본 명세: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)

토스 키에는 권한 범위(scope)가 없어서 이 키 하나로 주문까지 할 수 있다.
그래서 이 클라이언트는 아래 ALLOWED_PATHS의 GET 요청만 보낼 수 있게 막아 둔다 (토큰 발급만 예외).
주문·조건주문 API는 의도적으로 구현하지 않는다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from stockmate.importers import HoldingRow
from stockmate.models import Market

BASE_URL = "https://openapi.tossinvest.com"

# 조회 전용: 이 경로의 GET만 보낸다.
ALLOWED_PATHS = frozenset(
    {
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/buying-power",
        "/api/v1/prices",
        "/api/v1/exchange-rate",
    }
)

# 토큰은 클라이언트당 1개만 유효하다 (새로 받으면 이전 토큰은 바로 무효). 파일에 캐시해서 재사용한다.
TOKEN_REFRESH_MARGIN_SECONDS = 600
# 계좌 목록은 초당 1회, 나머지도 초당 3~6회 제한이 있어 호출 간격을 벌린다.
MIN_CALL_INTERVAL = 0.35
RETRIES = 2
_MARKETS = {"KR": Market.KRX, "US": Market.US}


class TossError(Exception):
    pass


@dataclass(frozen=True)
class TossBalance:
    holdings: list[HoldingRow]
    cash_krw: Decimal
    cash_usd: Decimal
    skipped: list[str]


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        account_seq: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._token_path = token_path
        self._account_seq = account_seq
        self._http = httpx.Client(base_url=BASE_URL, timeout=15, transport=transport)
        self._lock = threading.Lock()
        self._last_call = 0.0

    # ── 인증 ──────────────────────────────────────────

    def _token(self, renew: bool = False) -> str:
        if not renew:
            try:
                cached = json.loads(self._token_path.read_text())
                if cached["expires_at"] - time.time() > TOKEN_REFRESH_MARGIN_SECONDS:
                    return cached["access_token"]
            except (FileNotFoundError, KeyError, ValueError, TypeError):
                pass

        response = self._http.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        body = _json(response)
        if response.status_code != 200 or "access_token" not in body:
            detail = body.get("error_description") or body.get("error") or response.text[:200]
            raise TossError(f"토스 토큰 발급 실패 ({response.status_code}): {detail}")
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {"access_token": body["access_token"], "expires_at": time.time() + int(body["expires_in"])}
            )
        )
        os.chmod(self._token_path, 0o600)
        return body["access_token"]

    # ── 요청 ──────────────────────────────────────────

    def _get(self, path: str, params: dict[str, str] | None = None, account: bool = False) -> Any:
        if path not in ALLOWED_PATHS:
            raise TossError(f"조회 전용 클라이언트라 호출할 수 없는 경로예요: {path}")
        headers = {}
        if account:
            headers["X-Tossinvest-Account"] = str(self.account_seq())
        renewed = renew_now = False
        for attempt in range(RETRIES + 1):
            with self._lock:
                wait = self._last_call + MIN_CALL_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            response = self._http.get(
                path,
                params=params,
                headers={**headers, "Authorization": f"Bearer {self._token(renew=renew_now)}"},
            )
            renew_now = False
            if response.status_code == 429 and attempt < RETRIES:
                time.sleep(min(float(response.headers.get("Retry-After") or 1), 5))
                continue
            error = _json(response).get("error") or {}
            if response.status_code == 401 and not renewed and error.get("code") in (
                "expired-token",
                "token-revoked",
                "invalid-token",
            ):
                # 다른 곳에서 같은 키로 토큰을 새로 받았거나 만료됐다: 한 번만 새로 받아 다시 시도한다.
                renewed = renew_now = True
                continue
            break
        if response.status_code != 200:
            error = _json(response).get("error") or {}
            raise TossError(_explain(response.status_code, error, path))
        return _json(response)["result"]

    def account_seq(self) -> int:
        """종합매매 계좌의 accountSeq. .env에 없으면 계좌 목록에서 찾는다."""
        if self._account_seq is None:
            accounts = self._get("/api/v1/accounts")
            brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"]
            if not brokerage:
                raise TossError("토스 종합매매 계좌를 찾지 못했어요.")
            self._account_seq = int(brokerage[0]["accountSeq"])
        return self._account_seq

    # ── 조회 ──────────────────────────────────────────

    def balance(self) -> TossBalance:
        """보유 종목(평균단가는 토스가 계산한 매수 평균가)과 주문 가능 현금."""
        result = self._get("/api/v1/holdings", account=True)
        rows, skipped = [], []
        for item in result.get("items", []):
            market = _MARKETS.get(item.get("marketCountry"))
            if market is None:
                skipped.append(item.get("name") or item.get("symbol"))
                continue
            quantity = Decimal(item["quantity"])
            if quantity <= 0:
                continue
            rows.append(
                HoldingRow(
                    symbol=item["symbol"].upper(),
                    quantity=quantity,
                    avg_cost=Decimal(item["averagePurchasePrice"]),
                    name=item.get("name"),
                    market=market,
                )
            )
        cash = {}
        for currency in ("KRW", "USD"):
            power = self._get("/api/v1/buying-power", {"currency": currency}, account=True)
            cash[currency] = Decimal(power.get("cashBuyingPower") or 0)
        return TossBalance(rows, cash["KRW"], cash["USD"], skipped)

    def usd_krw(self) -> Decimal:
        result = self._get("/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"})
        return Decimal(result["midRate"])


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _explain(status: int, error: dict[str, Any], path: str) -> str:
    code, message = error.get("code", ""), error.get("message", "")
    if status == 403 and code == "edge-blocked":
        return (
            "토스가 이 서버의 IP를 막았어요. 토스증권 WTS 설정 > Open API > 허용 IP 관리에 "
            "이 서버의 공인 IP를 등록했는지 확인하세요."
        )
    if code == "token-revoked":
        return (
            "다른 곳에서 같은 토스 키로 토큰을 새로 받아서 이 토큰이 무효가 됐어요. "
            "토스 키는 한 곳에서만 쓰세요."
        )
    return f"토스 {path} 조회 실패 ({status} {code}): {message}"
