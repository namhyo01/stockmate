"""포트폴리오 핵심 로직: 계좌·종목 관리, 매매 기록, 평가."""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate.market_data import PriceProvider
from stockmate.models import Account, AccountKind, Broker, Holding, Instrument, Market, Side, Trade

ZERO = Decimal(0)
AVG_COST_PRECISION = Decimal("0.000001")

# 국내 종목코드는 6자리이며 첫 글자는 숫자다 (신규 코드는 영문이 섞이기도 한다. 예: 0088M0)
_KRX_SYMBOL = re.compile(r"^\d[0-9A-Z]{5}$")


class PortfolioError(Exception):
    """사용자에게 그대로 보여줘도 되는 도메인 오류."""


def detect_market(symbol: str) -> Market:
    return Market.KRX if _KRX_SYMBOL.match(symbol.strip().upper()) else Market.US


# ── 계좌 ──────────────────────────────────────────────


def create_account(
    session: Session,
    name: str,
    broker: Broker,
    kind: AccountKind = AccountKind.GENERAL,
    monthly_contribution: Decimal | None = None,
) -> Account:
    if session.scalar(select(Account).where(Account.name == name)):
        raise PortfolioError(f"이미 있는 계좌 이름이에요: {name}")
    if monthly_contribution is not None and monthly_contribution < 0:
        raise PortfolioError("월 적립액은 음수일 수 없어요.")
    account = Account(
        name=name, broker=broker, kind=kind, monthly_contribution=monthly_contribution
    )
    session.add(account)
    session.flush()
    return account


def get_account(session: Session, ref: str) -> Account:
    """계좌 이름 또는 ID로 찾는다."""
    account = session.scalar(select(Account).where(Account.name == ref))
    if account is None and ref.isdigit():
        account = session.get(Account, int(ref))
    if account is None:
        raise PortfolioError(f"계좌를 찾을 수 없어요: {ref}")
    return account


# ── 종목 ──────────────────────────────────────────────


def get_or_create_instrument(
    session: Session, symbol: str, market: Market | None = None, name: str | None = None
) -> Instrument:
    symbol = symbol.strip().upper()
    market = market or detect_market(symbol)
    instrument = session.scalar(
        select(Instrument).where(Instrument.symbol == symbol, Instrument.market == market)
    )
    if instrument is None:
        instrument = Instrument(symbol=symbol, market=market, name=name)
        session.add(instrument)
        session.flush()
    elif name and not instrument.name:
        instrument.name = name
    return instrument


def find_instrument(session: Session, symbol: str) -> Instrument:
    instrument = session.scalar(select(Instrument).where(Instrument.symbol == symbol.strip().upper()))
    if instrument is None:
        raise PortfolioError(f"등록된 종목이 아니에요: {symbol}")
    return instrument


# ── 잔고와 매매 ────────────────────────────────────────


def _get_holding(session: Session, account: Account, instrument: Instrument) -> Holding | None:
    return session.scalar(
        select(Holding).where(
            Holding.account_id == account.id, Holding.instrument_id == instrument.id
        )
    )


def set_holding(
    session: Session,
    account: Account,
    instrument: Instrument,
    quantity: Decimal,
    avg_cost: Decimal,
) -> Holding | None:
    """잔고를 주어진 값으로 덮어쓴다. 수량이 0이면 잔고를 지우고 None을 돌려준다."""
    if quantity < 0 or avg_cost < 0:
        raise PortfolioError("수량과 평균단가는 음수일 수 없어요.")
    holding = _get_holding(session, account, instrument)
    if quantity == 0:
        if holding is not None:
            account.holdings.remove(holding)
        return None
    if holding is None:
        holding = Holding(account=account, instrument=instrument, quantity=quantity, avg_cost=avg_cost)
        session.add(holding)
    else:
        holding.quantity = quantity
        holding.avg_cost = avg_cost
    session.flush()
    return holding


def record_trade(
    session: Session,
    account: Account,
    instrument: Instrument,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    traded_at: date,
    fee: Decimal = ZERO,
    source: str = "manual",
    note: str | None = None,
) -> Trade:
    """매매를 기록하고 잔고(수량, 이동평균 단가)를 갱신한다."""
    if quantity <= 0 or price < 0 or fee < 0:
        raise PortfolioError("수량은 0보다 커야 하고, 가격과 수수료는 음수일 수 없어요.")

    holding = _get_holding(session, account, instrument)
    held_qty = holding.quantity if holding else ZERO
    held_avg = holding.avg_cost if holding else ZERO
    realized_pnl = None

    if side is Side.BUY:
        new_qty = held_qty + quantity
        new_avg = ((held_qty * held_avg + quantity * price) / new_qty).quantize(AVG_COST_PRECISION)
        set_holding(session, account, instrument, new_qty, new_avg)
    else:
        if quantity > held_qty:
            raise PortfolioError(
                f"{instrument.display_name} 보유 수량({held_qty})보다 많이 팔 수 없어요."
            )
        realized_pnl = (price - held_avg) * quantity - fee
        set_holding(session, account, instrument, held_qty - quantity, held_avg)

    trade = Trade(
        account=account,
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        traded_at=traded_at,
        realized_pnl=realized_pnl,
        source=source,
        note=note,
    )
    session.add(trade)
    session.flush()
    return trade


# ── 평가 ──────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """평가가 끝난 보유 종목 한 건. 금액은 종목 통화 기준이고 *_krw는 원화 환산값이다."""

    account: Account
    instrument: Instrument
    quantity: Decimal
    avg_cost: Decimal
    price: Decimal | None
    fx_rate: Decimal

    @property
    def cost(self) -> Decimal:
        return self.quantity * self.avg_cost

    @property
    def value(self) -> Decimal | None:
        return None if self.price is None else self.quantity * self.price

    @property
    def cost_krw(self) -> Decimal:
        return self.cost * self.fx_rate

    @property
    def value_krw(self) -> Decimal | None:
        return None if self.value is None else self.value * self.fx_rate

    @property
    def pnl_krw(self) -> Decimal | None:
        return None if self.value_krw is None else self.value_krw - self.cost_krw

    @property
    def pnl_pct(self) -> Decimal | None:
        if self.pnl_krw is None or self.cost_krw == 0:
            return None
        return self.pnl_krw / self.cost_krw * 100


@dataclass(frozen=True)
class Totals:
    """현재가를 가져온 종목만 합산한다."""

    cost_krw: Decimal
    value_krw: Decimal
    count: int

    @property
    def pnl_krw(self) -> Decimal:
        return self.value_krw - self.cost_krw

    @property
    def pnl_pct(self) -> Decimal | None:
        return None if self.cost_krw == 0 else self.pnl_krw / self.cost_krw * 100


def build_positions(session: Session, provider: PriceProvider) -> list[Position]:
    holdings = session.scalars(
        select(Holding)
        .join(Holding.account)
        .join(Holding.instrument)
        .order_by(Account.name, Instrument.market, Instrument.symbol)
    ).all()
    prices = provider.last_prices({(h.instrument.market, h.instrument.symbol) for h in holdings})
    fx_rates = {c: provider.fx_to_krw(c) for c in {h.instrument.currency for h in holdings}}
    return [
        Position(
            account=h.account,
            instrument=h.instrument,
            quantity=h.quantity,
            avg_cost=h.avg_cost,
            price=prices.get((h.instrument.market, h.instrument.symbol)),
            fx_rate=fx_rates[h.instrument.currency],
        )
        for h in holdings
    ]


def totals(positions: Iterable[Position]) -> Totals:
    priced = [p for p in positions if p.value_krw is not None]
    return Totals(
        cost_krw=sum((p.cost_krw for p in priced), ZERO),
        value_krw=sum((p.value_krw for p in priced), ZERO),
        count=len(priced),
    )


def group_totals[K: Hashable](
    positions: Iterable[Position], key: Callable[[Position], K]
) -> dict[K, Totals]:
    groups: dict[K, list[Position]] = {}
    for position in positions:
        groups.setdefault(key(position), []).append(position)
    return {k: totals(v) for k, v in groups.items()}
