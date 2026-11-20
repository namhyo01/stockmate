"""증권사 API 동기화. CLI와 웹 대시보드에서 같이 쓴다."""

from __future__ import annotations

import logging

from dataclasses import dataclass, field, replace
from decimal import Decimal

import httpx

from sqlalchemy.orm import Session

from stockmate import importers, portfolio
from stockmate.brokers.kis import KisClient, KisError, KisMarketData
from stockmate.brokers.toss import TossClient, TossError
from stockmate.config import settings
from stockmate.market_data import (
    ExtendedHours,
    PriceProvider,
    PublicMarketData,
    download_extended,
    extended_prices,
    us_session_of,
)
from stockmate.models import Account, AccountKind, Broker, Market
from stockmate.portfolio import PortfolioError

log = logging.getLogger(__name__)


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
    """한투 API가 설정돼 있으면 한투 시세를, 아니면 공개 데이터를 쓴다. 미국 정규장 밖에는 프리·애프터·데이마켓 가격."""
    extended = lambda symbols: extended_prices(symbols, extended_download)  # noqa: E731
    if not kis_configured():
        return ExtendedHours(PublicMarketData(), extended)
    return ExtendedHours(KisMarketData(
        make_kis_client(), PublicMarketData(), settings.db_path.parent / "kis_exchanges.json"
    ), extended)


_price_client: TossClient | None = None


def extended_download(symbols: list[str], toss=None, fallback=download_extended) -> dict[str, tuple[Decimal, str]]:
    """미국 정규장 밖 가격: 토스 시세(실시간, 데이마켓 포함)를 먼저, 토스가 모르는 종목은 yfinance 1분봉.

    토스가 답한 종목의 마지막 체결이 정규장 시간이면 정규장 밖 거래가 아직 없는 것이라 넣지 않는다.
    """
    global _price_client
    result: dict[str, tuple[Decimal, str]] = {}
    answered: set[str] = set()
    if toss is None and toss_configured():
        toss = _price_client = _price_client or make_toss_client()
    if toss is not None:
        try:
            for symbol, (price, at) in toss.prices(symbols).items():
                answered.add(symbol)
                if session := us_session_of(at):
                    result[symbol] = (price, session)
        except (TossError, httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("토스 시세 조회 실패: %s", e)
    if missing := [s for s in symbols if s not in answered]:
        result.update(fallback(missing))
    return result


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


def toss_configured() -> bool:
    return bool(settings.toss_client_id and settings.toss_client_secret)


def make_toss_client() -> TossClient:
    if not toss_configured():
        raise TossError(".env에 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET을 채워주세요.")
    return TossClient(
        settings.toss_client_id,
        settings.toss_client_secret,
        settings.db_path.parent / "toss_token.json",
        settings.toss_account_seq,
    )


def api_synced(account: Account) -> bool:
    """증권사 API로 자동 동기화하는 계좌인가 (아니면 스크린샷으로 갱신한다)."""
    if account.broker is Broker.KIS:
        return True
    return account.broker is Broker.TOSS and account.kind is AccountKind.GENERAL and toss_configured()


def sync_toss(session: Session, account_name: str = "토스", client: TossClient | None = None) -> SyncResult:
    """토스증권 종합매매 계좌의 보유 종목(평균단가 포함)과 주문 가능 현금을 가져와 통째로 동기화한다."""
    client = client or make_toss_client()
    balance = client.balance()
    created = False
    try:
        account = portfolio.get_account(session, account_name)
    except PortfolioError:
        account = portfolio.create_account(session, account_name, Broker.TOSS)
        created = True
    if account.broker is not Broker.TOSS:
        raise PortfolioError(f"{account.name}은 토스증권 계좌가 아니에요.")

    fx = client.usd_krw()
    existing = {(h.instrument.market, h.instrument.symbol): h for h in account.holdings}
    rows = [
        replace(r, avg_fx=portfolio.blend_avg_fx(existing.get((r.market, r.symbol)), r.quantity, r.avg_cost, fx))
        if r.market is Market.US
        else r
        for r in balance.holdings
    ]
    result = importers.import_holdings(session, account, rows, replace=True)
    account.cash, account.cash_usd = balance.cash_krw, balance.cash_usd
    warnings = []
    if balance.skipped:
        warnings.append(f"미국·국내 외 종목은 아직 지원하지 않아 건너뛰었어요: {', '.join(balance.skipped)}")
    return SyncResult(
        account=account,
        domestic=sum(1 for r in balance.holdings if r.market is Market.KRX),
        overseas=sum(1 for r in balance.holdings if r.market is Market.US),
        created=created,
        removed=[i.display_name for i in result.removed],
        warnings=warnings,
    )
