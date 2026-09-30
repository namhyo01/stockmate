"""증권사 API 동기화. CLI와 웹 대시보드에서 같이 쓴다."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from stockmate import importers, portfolio
from stockmate.brokers.kis import KisClient, KisError, KisMarketData
from stockmate.config import settings
from stockmate.market_data import PriceProvider, PublicMarketData
from stockmate.models import Account, AccountKind, Broker
from stockmate.portfolio import PortfolioError


@dataclass
class SyncResult:
    account: Account
    domestic: int
    overseas: int
    created: bool = False
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def kis_configured() -> bool:
    return bool(settings.kis_app_key and settings.kis_app_secret and settings.kis_account_no)


def make_kis_client() -> KisClient:
    if not kis_configured():
        raise KisError(".env에 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO를 채워주세요.")
    return KisClient(
        settings.kis_app_key,
        settings.kis_app_secret,
        settings.kis_account_no,
        settings.db_path.parent / "kis_token.json",
    )


def price_provider() -> PriceProvider:
    """한투 API가 설정돼 있으면 한투 시세를, 아니면 공개 데이터를 쓴다."""
    if not kis_configured():
        return PublicMarketData()
    return KisMarketData(
        make_kis_client(), PublicMarketData(), settings.db_path.parent / "kis_exchanges.json"
    )


def sync_kis(
    session: Session, account_name: str = "한투", kind: AccountKind = AccountKind.GENERAL
) -> SyncResult:
    """한국투자증권 잔고(국내+해외)와 예수금을 가져와 계좌를 통째로 동기화한다."""
    client = make_kis_client()
    domestic, cash = client.domestic_balance()
    warnings = []
    try:
        overseas, skipped = client.overseas_holdings()
    except KisError as e:
        # ISA·연금 계좌는 해외주식을 직접 살 수 없어서 해외 잔고 조회가 막혀 있을 수 있다.
        overseas, skipped = [], []
        warnings.append(f"해외 잔고는 건너뛰었어요: {e}")
    if skipped:
        warnings.append(f"미국 외 해외 종목은 아직 지원하지 않아 건너뛰었어요: {', '.join(skipped)}")

    created = False
    try:
        account = portfolio.get_account(session, account_name)
    except PortfolioError:
        account = portfolio.create_account(session, account_name, Broker.KIS, kind)
        created = True
    if account.broker is not Broker.KIS:
        raise PortfolioError(f"{account.name}은 한국투자증권 계좌가 아니에요.")

    result = importers.import_holdings(session, account, domestic + overseas, replace=True)
    account.cash = cash
    return SyncResult(
        account=account,
        domestic=len(domestic),
        overseas=len(overseas),
        created=created,
        removed=[i.display_name for i in result.removed],
        warnings=warnings,
    )
