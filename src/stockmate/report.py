"""대시보드와 AI 분석이 함께 쓰는 포트폴리오 스냅샷 (JSON으로 바로 보낼 수 있는 dict)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate import portfolio
from stockmate.market_data import PriceProvider
from stockmate.models import Account, Broker, Instrument, Market
from stockmate.portfolio import ZERO, Totals

# 국내 상장 ETF는 이름으로 실제 투자 지역을 추정한다 (예: TIGER 미국S&P500 → 미국).
_REGION_KEYWORDS = [
    ("미국", ("미국", "나스닥", "S&P", "다우존스", "필라델피아")),
    ("일본", ("일본", "니케이", "토픽스")),
    ("중국", ("중국", "차이나", "항셍", "CSI")),
    ("인도", ("인도", "니프티")),
    ("글로벌", ("글로벌", "선진국", "MSCI")),
]

_EMPTY = Totals(ZERO, ZERO, 0)


def exposure_region(instrument: Instrument) -> str:
    if instrument.market is Market.US:
        return "미국"
    name = instrument.name or ""
    for region, keywords in _REGION_KEYWORDS:
        if any(keyword in name for keyword in keywords):
            return region
    return "한국"


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _totals(t: Totals) -> dict[str, Any]:
    return {
        "cost": _num(t.cost_krw),
        "value": _num(t.value_krw),
        "pnl": _num(t.pnl_krw),
        "pnl_pct": _num(t.pnl_pct),
        "count": t.count,
    }


def build_snapshot(session: Session, provider: PriceProvider) -> dict[str, Any]:
    positions = sorted(
        portfolio.build_positions(session, provider),
        key=lambda p: p.value_krw if p.value_krw is not None else p.cost_krw,
        reverse=True,
    )
    accounts = session.scalars(select(Account).order_by(Account.id)).all()
    grand = portfolio.totals(positions)
    by_account = portfolio.group_totals(positions, lambda p: p.account.id)
    by_region = portfolio.group_totals(positions, lambda p: exposure_region(p.instrument))
    cash = sum((a.cash or ZERO for a in accounts), ZERO)

    def weight(value: Decimal | None) -> float | None:
        if value is None or not grand.value_krw:
            return None
        return float(value / grand.value_krw * 100)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fx": (
            {"USD": _num(provider.fx_to_krw("USD"))}
            if any(p.instrument.currency == "USD" for p in positions)
            else {}
        ),
        "total": {**_totals(grand), "cash": _num(cash), "assets": _num(grand.value_krw + cash)},
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "broker": a.broker.value,
                "broker_label": a.broker.label,
                "kind": a.kind.value,
                "kind_label": a.kind.label,
                "monthly_contribution": _num(a.monthly_contribution),
                "cash": _num(a.cash),
                "synced_at": a.synced_at.isoformat(timespec="minutes") if a.synced_at else None,
                "sync_method": "api" if a.broker is Broker.KIS else "screenshot",
                **_totals(by_account.get(a.id, _EMPTY)),
            }
            for a in accounts
        ],
        "regions": [
            {"region": region, **_totals(t), "weight": weight(t.value_krw)}
            for region, t in sorted(by_region.items(), key=lambda kv: kv[1].value_krw, reverse=True)
        ],
        "positions": [
            {
                "account_id": p.account.id,
                "account": p.account.name,
                "symbol": p.instrument.symbol,
                "name": p.instrument.display_name,
                "market": p.instrument.market.value,
                "currency": p.instrument.currency,
                "region": exposure_region(p.instrument),
                "strategy": p.instrument.strategy.value if p.instrument.strategy else None,
                "strategy_label": p.instrument.strategy.label if p.instrument.strategy else None,
                "quantity": _num(p.quantity),
                "avg_cost": _num(p.avg_cost),
                "price": _num(p.price),
                "cost_krw": _num(p.cost_krw),
                "value_krw": _num(p.value_krw),
                "pnl_krw": _num(p.pnl_krw),
                "pnl_pct": _num(p.pnl_pct),
                "weight": weight(p.value_krw),
            }
            for p in positions
        ],
    }
