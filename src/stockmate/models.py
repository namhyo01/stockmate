"""DB 모델: 계좌, 종목, 보유 잔고, 매매 기록."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class Broker(StrEnum):
    KIS = "kis"
    TOSS = "toss"
    SAMSUNG = "samsung"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {"kis": "한국투자", "toss": "토스", "samsung": "삼성", "other": "기타"}[self.value]


class AccountKind(StrEnum):
    GENERAL = "general"
    PENSION = "pension"  # 연금저축: ETF·펀드만 매수 가능
    IRP = "irp"  # 개인형 퇴직연금: ETF·펀드만, 위험자산 70% 한도
    ISA = "isa"

    @property
    def label(self) -> str:
        return {"general": "일반", "pension": "연금저축", "irp": "IRP", "isa": "ISA"}[self.value]


class Market(StrEnum):
    KRX = "KRX"
    US = "US"

    @property
    def currency(self) -> str:
        return "KRW" if self is Market.KRX else "USD"


class Strategy(StrEnum):
    LONG = "long"
    SWING = "swing"
    SHORT = "short"

    @property
    def label(self) -> str:
        return {"long": "장기", "swing": "스윙", "short": "단기"}[self.value]


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class DecimalText(TypeDecorator[Decimal]):
    """SQLite는 Decimal을 네이티브로 지원하지 않으므로 정밀도 손실이 없도록 문자열로 저장한다."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


def _enum(cls: type[StrEnum]) -> Enum:
    return Enum(cls, native_enum=False, values_callable=lambda e: [m.value for m in e], length=20)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    broker: Mapped[Broker] = mapped_column(_enum(Broker))
    kind: Mapped[AccountKind] = mapped_column(_enum(AccountKind), default=AccountKind.GENERAL)
    # 매달 넣는 금액 (원). 적립식 매수 계획에 쓴다.
    monthly_contribution: Mapped[Decimal | None] = mapped_column(DecimalText())
    # 예수금 (원). API로 동기화하는 계좌만 채워진다.
    cash: Mapped[Decimal | None] = mapped_column(DecimalText())
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    holdings: Mapped[list[Holding]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    market: Mapped[Market] = mapped_column(_enum(Market))
    name: Mapped[str | None] = mapped_column(String(100))
    strategy: Mapped[Strategy | None] = mapped_column(_enum(Strategy))

    @property
    def currency(self) -> str:
        return self.market.currency

    @property
    def display_name(self) -> str:
        return self.name or self.symbol


class Holding(Base):
    """계좌별 현재 보유 잔고. 평균단가는 증권사 앱 표기와 같게 수수료를 제외한 값이다."""

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("account_id", "instrument_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    quantity: Mapped[Decimal] = mapped_column(DecimalText())
    avg_cost: Mapped[Decimal] = mapped_column(DecimalText())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="holdings")
    instrument: Mapped[Instrument] = relationship(lazy="joined")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    side: Mapped[Side] = mapped_column(_enum(Side))
    quantity: Mapped[Decimal] = mapped_column(DecimalText())
    price: Mapped[Decimal] = mapped_column(DecimalText())
    fee: Mapped[Decimal] = mapped_column(DecimalText(), default=Decimal(0))
    traded_at: Mapped[date] = mapped_column(Date)
    # 매도 시점의 실현손익 (종목 통화 기준, 수수료 차감 후)
    realized_pnl: Mapped[Decimal | None] = mapped_column(DecimalText())
    source: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[Account] = relationship()
    instrument: Mapped[Instrument] = relationship(lazy="joined")


class Recommendation(Base):
    """Claude 분석 리포트의 종목별 의견. 다음 분석에서 '지난 추천 이후 가격 변화'를 보여주는 데 쓴다."""

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(100))
    account: Mapped[str | None] = mapped_column(String(50))
    opinion: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(500))
    # 추천 당시 현재가와 관심 가격대 (종목 통화 기준)
    price: Mapped[Decimal | None] = mapped_column(DecimalText())
    currency: Mapped[str | None] = mapped_column(String(3))
    watch_low: Mapped[Decimal | None] = mapped_column(DecimalText())
    watch_high: Mapped[Decimal | None] = mapped_column(DecimalText())

