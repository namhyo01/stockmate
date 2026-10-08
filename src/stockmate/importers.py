"""CSV로 보유 잔고 가져오기.

증권사별 내보내기 형식은 샘플 파일을 받는 대로 추가하고, 지금은 공통 템플릿 형식만 지원한다.
    symbol,name,quantity,avg_cost,market   (name과 market은 생략 가능)
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from stockmate.models import Account, Instrument, Market
from stockmate.portfolio import PortfolioError, get_or_create_instrument, set_holding

TEMPLATE = """symbol,name,quantity,avg_cost,market
005930,삼성전자,10,71000,KRX
AAPL,Apple,3.5,180.25,US
"""

_REQUIRED_COLUMNS = {"symbol", "quantity", "avg_cost"}


@dataclass(frozen=True)
class HoldingRow:
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    name: str | None = None
    market: Market | None = None
    avg_fx: Decimal | None = None  # 외화 종목의 매입 평균 환율 (모르면 None: 기존 값 유지)


@dataclass
class ImportResult:
    imported: list[Instrument] = field(default_factory=list)
    removed: list[Instrument] = field(default_factory=list)


def _parse_decimal(text: str | None) -> Decimal:
    return Decimal((text or "").replace(",", "").strip())


def parse_holdings_csv(text: str) -> list[HoldingRow]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise PortfolioError(f"CSV에 필요한 열이 없어요: {', '.join(sorted(missing))}")

    rows = []
    for line_no, raw in enumerate(reader, start=2):
        symbol = (raw.get("symbol") or "").strip()
        if not symbol:
            continue
        try:
            quantity = _parse_decimal(raw["quantity"])
            avg_cost = _parse_decimal(raw["avg_cost"])
        except InvalidOperation:
            raise PortfolioError(f"{line_no}번째 줄의 숫자를 읽을 수 없어요: {dict(raw)}") from None
        market_text = (raw.get("market") or "").strip().upper()
        try:
            market = Market(market_text) if market_text else None
        except ValueError:
            raise PortfolioError(
                f"{line_no}번째 줄의 market은 KRX 또는 US여야 해요: {market_text}"
            ) from None
        name = (raw.get("name") or "").strip() or None
        rows.append(HoldingRow(symbol, quantity, avg_cost, name, market))
    return rows


def import_holdings(
    session: Session, account: Account, rows: list[HoldingRow], replace: bool = False
) -> ImportResult:
    """행마다 잔고를 덮어쓴다. replace=True면 CSV에 없는 기존 잔고는 지운다 (계좌 전체 동기화)."""
    result = ImportResult()
    for row in rows:
        instrument = get_or_create_instrument(session, row.symbol, row.market, row.name)
        set_holding(session, account, instrument, row.quantity, row.avg_cost, row.avg_fx)
        result.imported.append(instrument)

    if replace:
        imported_ids = {i.id for i in result.imported}
        for holding in list(account.holdings):
            if holding.instrument_id not in imported_ids:
                result.removed.append(holding.instrument)
                account.holdings.remove(holding)
    account.synced_at = datetime.now()
    session.flush()
    return result
