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
    # 원화 예수금. 증권사 API나 스크린샷(MMF 등 현금성 상품 포함)으로 채워진다.
    cash: Mapped[Decimal | None] = mapped_column(DecimalText())
    # 달러 예수금 (토스처럼 달러를 따로 들고 있는 계좌). 원화 환산은 볼 때의 환율로 한다.
    cash_usd: Mapped[Decimal | None] = mapped_column(DecimalText())
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
    # 외화 종목을 살 때의 평균 환율 (1 외화 = ? 원). 원화 투자 원금 = 수량 × 평균단가 × avg_fx 라서
    # 원화 손익에 환율 손익까지 들어간다. 모르면 비워 두고 현재 환율로 계산한다.
    avg_fx: Mapped[Decimal | None] = mapped_column(DecimalText())
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



class PlanItem(Base):
    """Claude와 정한 매매 계획 한 건 (무엇을, 얼마나, 어떤 가격·시기에 사고팔지).

    대화나 분석 응답의 actions 블록에서 생기고, 사용자가 '했어요'/'취소'로 닫는다.
    """

    __tablename__ = "plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20))  # chat | analysis | rebuild
    side: Mapped[Side] = mapped_column(_enum(Side))
    name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str | None] = mapped_column(String(20))
    market: Mapped[Market | None] = mapped_column(_enum(Market))
    account: Mapped[str | None] = mapped_column(String(50))
    quantity: Mapped[Decimal | None] = mapped_column(DecimalText())
    amount_krw: Mapped[Decimal | None] = mapped_column(DecimalText())
    # 이 가격 범위에 들어오면 실행 (종목 통화 기준, 한쪽만 있어도 된다)
    price_low: Mapped[Decimal | None] = mapped_column(DecimalText())
    price_high: Mapped[Decimal | None] = mapped_column(DecimalText())
    timing: Mapped[str | None] = mapped_column(String(200))
    # 실행할 날짜 범위. 계획 탭에서 시기별로 묶는 데 쓴다 (날짜와 상관없는 조건 계획은 비어 있다)
    due_from: Mapped[date | None] = mapped_column(Date)
    due_to: Mapped[date | None] = mapped_column(Date)
    # AI가 날짜를 정했는지 (날짜가 비어 있어도 '조건 계획'으로 판단을 마쳤으면 True). 같은 계획을 다시 판단하지 않게 한다
    due_checked: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | done | canceled | replaced
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Idea(Base):
    """AI가 새로 추천한 종목 (보유하지 않은 종목 위주). 추천 탭에 카드로 보인다.

    추천 당시 가격을 남겨서 '추천 뒤 얼마나 움직였는지'를 보여준다.
    """

    __tablename__ = "ideas"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20))  # chat | analysis | extract
    name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str | None] = mapped_column(String(20))
    market: Mapped[Market | None] = mapped_column(_enum(Market))
    account: Mapped[str | None] = mapped_column(String(50))  # 담을 계좌
    summary: Mapped[str | None] = mapped_column(String(300))  # 한 줄 요약
    thesis: Mapped[str | None] = mapped_column(String(3000))  # 추천 이유 (줄바꿈으로 여러 개)
    risks: Mapped[str | None] = mapped_column(String(2000))
    overlap: Mapped[str | None] = mapped_column(String(1000))  # 가진 종목과 겹치는 정도
    entry_low: Mapped[Decimal | None] = mapped_column(DecimalText())
    entry_high: Mapped[Decimal | None] = mapped_column(DecimalText())
    price_at: Mapped[Decimal | None] = mapped_column(DecimalText())  # 추천 당시 가격 (처음 조회한 값)
    sources: Mapped[str | None] = mapped_column(String(4000))  # JSON [{title, url}]
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | dismissed | replaced
