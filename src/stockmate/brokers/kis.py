"""한국투자증권 Open API 클라이언트 (조회 전용).

공식 예제: https://github.com/koreainvestment/open-trading-api
주문 API는 의도적으로 구현하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from stockmate.importers import HoldingRow
from stockmate.market_data import Key, PriceProvider, previous_closes
from stockmate.models import Market

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.koreainvestment.com:9443"

# 접근토큰은 24시간 유효하고 발급은 1분에 1번만 가능하므로 파일에 캐시한다.
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)

# 실전 계좌는 초당 20건까지 호출할 수 있다. 여유를 두고 호출 간격을 벌린다.
MIN_CALL_INTERVAL = 0.1
RATE_LIMIT_CODE = "EGW00201"
RATE_LIMIT_RETRIES = 2

# 미국 종목은 거래소 코드를 알아야 시세를 조회할 수 있다. 모르면 순서대로 시도한다.
US_EXCHANGES = ("NAS", "NYS", "AMS")


class KisError(Exception):
    pass


class KisClient:
    def __init__(self, app_key: str, app_secret: str, account_no: str, token_path: Path):
        digits = account_no.replace("-", "")
        if len(digits) != 10 or not digits.isdigit():
            raise KisError("KIS_ACCOUNT_NO는 '12345678-01' 형식이어야 해요.")
        self._cano, self._product_code = digits[:8], digits[8:]
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_path = token_path
        self._http = httpx.Client(base_url=BASE_URL, timeout=15)
        self._throttle_lock = threading.Lock()
        self._last_call = 0.0

    # ── 인증 ──────────────────────────────────────────

    def _token(self) -> str:
        cached = self._load_cached_token()
        if cached:
            return cached

        response = self._http.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
        )
        body = response.json()
        if response.status_code != 200 or "access_token" not in body:
            raise KisError(f"토큰 발급 실패: {body.get('error_description') or body}")

        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {"access_token": body["access_token"], "expires_at": body["access_token_token_expired"]}
            )
        )
        os.chmod(self._token_path, 0o600)
        return body["access_token"]

    def _load_cached_token(self) -> str | None:
        try:
            cached = json.loads(self._token_path.read_text())
            expires_at = datetime.strptime(cached["expires_at"], "%Y-%m-%d %H:%M:%S")
        except (FileNotFoundError, KeyError, ValueError):
            return None
        if expires_at - datetime.now() < TOKEN_REFRESH_MARGIN:
            return None
        return cached["access_token"]

    def _get(
        self, path: str, tr_id: str, params: dict[str, str], tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        """본문과 연속조회 여부(tr_cont 응답 헤더: M/F면 다음 페이지 있음)를 돌려준다."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            body, next_flag = self._request(path, tr_id, params, tr_cont)
            if body.get("msg_cd") != RATE_LIMIT_CODE or attempt == RATE_LIMIT_RETRIES:
                break
            time.sleep(1)  # 초당 호출 한도 초과: 잠시 쉬었다가 다시 시도
        if body.get("rt_cd") != "0":
            raise KisError(f"{tr_id} 조회 실패: [{body.get('msg_cd')}] {body.get('msg1')}")
        return body, next_flag

    def _request(
        self, path: str, tr_id: str, params: dict[str, str], tr_cont: str
    ) -> tuple[dict[str, Any], str]:
        with self._throttle_lock:
            wait = self._last_call + MIN_CALL_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        response = self._http.get(
            path,
            params=params,
            headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self._token()}",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
                "tr_id": tr_id,
                "tr_cont": tr_cont,
                "custtype": "P",
            },
        )
        return response.json(), response.headers.get("tr_cont", "")

    # ── 잔고 ──────────────────────────────────────────

    def domestic_balance(self) -> tuple[list[HoldingRow], Decimal]:
        """국내주식 잔고와 예수금 (주식잔고조회, TTTC8434R).

        예수금은 총평가금액 − 유가평가금액으로 구한다 (앱의 총자산과 맞추기 위해 D+2 정산분 포함).
        """
        rows: list[HoldingRow] = []
        fk, nk, tr_cont = "", "", ""
        while True:
            body, next_flag = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "TTTC8434R",
                {
                    "CANO": self._cano,
                    "ACNT_PRDT_CD": self._product_code,
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "",
                    "INQR_DVSN": "02",  # 종목별
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "00",  # 전일매매 포함
                    "CTX_AREA_FK100": fk,
                    "CTX_AREA_NK100": nk,
                },
                tr_cont,
            )
            for item in body.get("output1", []):
                quantity = Decimal(item["hldg_qty"])
                if quantity > 0:
                    rows.append(
                        HoldingRow(
                            symbol=item["pdno"],
                            quantity=quantity,
                            avg_cost=Decimal(item["pchs_avg_pric"]).normalize(),
                            name=item.get("prdt_name") or None,
                            market=Market.KRX,
                        )
                    )
            if next_flag not in ("M", "F"):
                summary = (body.get("output2") or [{}])[0]
                cash = Decimal(summary.get("tot_evlu_amt") or 0) - Decimal(
                    summary.get("scts_evlu_amt") or 0
                )
                return rows, cash
            fk, nk, tr_cont = body["ctx_area_fk100"], body["ctx_area_nk100"], "N"

    def overseas_holdings(self) -> tuple[list[HoldingRow], list[str]]:
        """해외주식 잔고 (체결기준현재잔고, CTRP6504R). 미국 외 종목은 두 번째 값으로 돌려준다."""
        body, _ = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            "CTRP6504R",
            {
                "CANO": self._cano,
                "ACNT_PRDT_CD": self._product_code,
                "WCRC_FRCR_DVSN_CD": "02",  # 외화 기준
                "NATN_CD": "000",  # 전체 국가
                "TR_MKET_CD": "00",  # 전체 시장
                "INQR_DVSN_CD": "00",
            },
        )
        rows: list[HoldingRow] = []
        skipped: list[str] = []
        for item in body.get("output1", []):
            quantity = Decimal(item["cblc_qty13"])
            if quantity <= 0:
                continue
            if item.get("buy_crcy_cd") != "USD":
                skipped.append(f"{item.get('prdt_name') or item['pdno']}({item.get('buy_crcy_cd')})")
                continue
            rows.append(
                HoldingRow(
                    symbol=item["pdno"],
                    quantity=quantity,
                    avg_cost=Decimal(item["avg_unpr3"]).normalize(),
                    name=item.get("prdt_name") or None,
                    market=Market.US,
                )
            )
        return rows, skipped

    # ── 시세 ──────────────────────────────────────────

    def domestic_quote(self, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        """국내 (현재가, 전일 종가) (주식현재가 시세, FHKST01010100). stck_sdpr는 기준가(전일 종가)."""
        body, _ = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        output = body.get("output", {})
        return _positive(output.get("stck_prpr")), _positive(output.get("stck_sdpr"))

    def overseas_quote(self, exchange: str, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        """해외 (현재가, 전일 종가) (해외주식 현재체결가, HHDFS00000300). 해당 거래소에 없으면 현재가가 None."""
        body, _ = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            "HHDFS00000300",
            {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        output = body.get("output", {})
        return _positive(output.get("last")), _positive(output.get("base"))


class KisMarketData:
    """한국투자증권 시세 API로 현재가를 가져온다 (어느 증권사에서 산 종목이든 조회 가능).

    한투에서 못 가져온 종목과 환율은 공개 데이터(fallback)로 채운다.
    """

    def __init__(self, client: KisClient, fallback: PriceProvider, exchange_cache: Path):
        self._client = client
        self._fallback = fallback
        self._exchange_cache = exchange_cache
        self._prev: dict[Key, Decimal] = {}
        try:
            self._exchanges: dict[str, str] = json.loads(exchange_cache.read_text())
        except (FileNotFoundError, ValueError):
            self._exchanges = {}

    def last_prices(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        keys = set(keys)
        prices: dict[Key, Decimal] = {}
        for market, symbol in sorted(keys):
            try:
                price, prev = (
                    self._client.domestic_quote(symbol)
                    if market is Market.KRX
                    else self._us_quote(symbol)
                )
            except (KisError, httpx.HTTPError) as e:
                log.warning("한투 시세 조회 실패 %s: %s", symbol, e)
                price = prev = None
            if price is not None:
                prices[(market, symbol)] = price
                if prev is not None:
                    self._prev[(market, symbol)] = prev
        if missing := keys - prices.keys():
            prices.update(self._fallback.last_prices(missing))
            self._prev.update(previous_closes(self._fallback, missing))
        return prices

    def previous_closes(self, keys: Iterable[Key]) -> dict[Key, Decimal]:
        return {k: self._prev[k] for k in keys if k in self._prev}

    def fx_to_krw(self, currency: str) -> Decimal:
        return self._fallback.fx_to_krw(currency)

    def _us_quote(self, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        known = self._exchanges.get(symbol)
        for exchange in ([known] if known else []) + [e for e in US_EXCHANGES if e != known]:
            price, prev = self._client.overseas_quote(exchange, symbol)
            if price is not None:
                if exchange != known:
                    self._exchanges[symbol] = exchange
                    self._exchange_cache.write_text(json.dumps(self._exchanges, indent=1))
                return price, prev
        return None, None


def _positive(text: str | None) -> Decimal | None:
    value = Decimal(text or 0)
    return value if value > 0 else None
