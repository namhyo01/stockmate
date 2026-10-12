"""명령줄 인터페이스. 사용법: `uv run stock --help`"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Column, Table
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockmate import importers, portfolio, sync
from stockmate.brokers.kis import KisError
from stockmate.brokers.toss import TossError
from stockmate.config import settings
from stockmate.db import session_scope
from stockmate.market_data import MarketDataError, lookup_name
from stockmate.models import Account, AccountKind, Broker, Holding, Instrument, Market, Side, Strategy, Trade
from stockmate.portfolio import PortfolioError, Position, Totals

app = typer.Typer(
    help="📈 stockmate: 여러 증권사 계좌를 한곳에서 관리하는 개인 투자 비서",
    no_args_is_help=True,
)
account_app = typer.Typer(help="계좌 관리", no_args_is_help=True)
holding_app = typer.Typer(help="보유 잔고 직접 입력", no_args_is_help=True)
trade_app = typer.Typer(help="매매 기록", no_args_is_help=True)
import_app = typer.Typer(help="CSV 가져오기", no_args_is_help=True)
sync_app = typer.Typer(help="증권사 API로 잔고 동기화", no_args_is_help=True)
app.add_typer(account_app, name="account")
app.add_typer(holding_app, name="holding")
app.add_typer(trade_app, name="trade")
app.add_typer(import_app, name="import")
app.add_typer(sync_app, name="sync")

console = Console()

DecimalArg = Annotated[Decimal, typer.Argument(parser=Decimal)]
MarketOpt = Annotated[
    Market | None, typer.Option("--market", "-m", help="생략하면 종목코드로 자동 판별")
]
NameOpt = Annotated[str | None, typer.Option("--name", help="생략하면 자동 조회")]


def main() -> None:
    try:
        app()
    except (PortfolioError, MarketDataError, KisError, TossError) as e:
        console.print(f"[bold red]오류:[/] {e}")
        raise SystemExit(1) from None


# ── 출력 도우미 ─────────────────────────────────────────


def _qty(value: Decimal) -> str:
    return f"{value.normalize():,f}"


def _money(value: Decimal | None, currency: str = "KRW") -> str:
    if value is None:
        return "[dim]-[/]"
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.0f}"


def _signed(value: Decimal | None, text: str) -> str:
    """한국식 색상: 상승은 빨강, 하락은 파랑."""
    if value is None:
        return "[dim]-[/]"
    color = "red" if value > 0 else "blue" if value < 0 else "default"
    return f"[{color}]{text}[/]"


def _pnl(value: Decimal | None) -> str:
    return _signed(value, f"{value:+,.0f}" if value is not None else "")


def _pct(value: Decimal | None) -> str:
    return _signed(value, f"{value:+.2f}%" if value is not None else "")


def _resolve_instrument(
    session: Session, symbol: str, market: Market | None, name: str | None
) -> Instrument:
    instrument = portfolio.get_or_create_instrument(session, symbol, market, name)
    if not instrument.name:
        with console.status(f"{instrument.symbol} 종목명 조회 중..."):
            instrument.name = lookup_name(instrument.symbol, instrument.market)
    return instrument


# ── 계좌 ──────────────────────────────────────────────


@account_app.command("add")
def account_add(
    name: Annotated[str, typer.Argument(help="계좌 별칭 (예: 토스, 한투-ISA)")],
    broker: Annotated[Broker, typer.Option("--broker", "-b", help="증권사")],
    kind: Annotated[
        AccountKind, typer.Option("--kind", "-k", help="계좌 종류")
    ] = AccountKind.GENERAL,
    monthly: Annotated[
        Decimal | None, typer.Option("--monthly", parser=Decimal, help="월 적립액 (원)")
    ] = None,
) -> None:
    """계좌를 등록한다."""
    with session_scope() as session:
        account = portfolio.create_account(session, name, broker, kind, monthly)
        extra = f", 매달 {_money(monthly)}원 적립" if monthly else ""
        console.print(
            f"✅ [{account.id}] {account.name} ({broker.label} {kind.label}{extra}) 계좌를 등록했어요."
        )


@account_app.command("list")
def account_list() -> None:
    """등록된 계좌를 보여준다."""
    with session_scope() as session:
        rows = session.execute(
            select(Account, func.count(Holding.id))
            .outerjoin(Holding)
            .group_by(Account.id)
            .order_by(Account.id)
        ).all()
        if not rows:
            console.print("등록된 계좌가 없어요. `stock account add 이름 --broker toss`로 추가하세요.")
            return
        table = Table("ID", "이름", "증권사", "종류", "월 적립액", "보유 종목 수")
        for account, count in rows:
            table.add_row(
                str(account.id),
                account.name,
                account.broker.label,
                account.kind.label,
                _money(account.monthly_contribution),
                str(count),
            )
        console.print(table)


# ── 잔고 직접 입력 ──────────────────────────────────────


@holding_app.command("set")
def holding_set(
    account: Annotated[str, typer.Argument(help="계좌 이름 또는 ID")],
    symbol: Annotated[str, typer.Argument(help="종목코드 (예: 005930, AAPL)")],
    quantity: DecimalArg,
    avg_cost: DecimalArg,
    market: MarketOpt = None,
    name: NameOpt = None,
) -> None:
    """현재 보유 잔고를 그대로 입력한다. 수량을 0으로 주면 잔고를 지운다."""
    with session_scope() as session:
        acct = portfolio.get_account(session, account)
        instrument = _resolve_instrument(session, symbol, market, name)
        portfolio.set_holding(session, acct, instrument, quantity, avg_cost)
        console.print(
            f"✅ {acct.name} · {instrument.display_name}: {_qty(quantity)}주 @ "
            f"{_money(avg_cost, instrument.currency)}"
        )


# ── 매매 기록 ──────────────────────────────────────────


def _trade(side: Side) -> Callable[..., None]:
    def command(
        account: Annotated[str, typer.Argument(help="계좌 이름 또는 ID")],
        symbol: Annotated[str, typer.Argument(help="종목코드 (예: 005930, AAPL)")],
        quantity: DecimalArg,
        price: DecimalArg,
        traded_at: Annotated[
            date | None,
            typer.Option("--date", "-d", parser=date.fromisoformat, help="YYYY-MM-DD, 기본값 오늘"),
        ] = None,
        fee: Annotated[Decimal, typer.Option(parser=Decimal, help="수수료+세금")] = Decimal(0),
        market: MarketOpt = None,
        name: NameOpt = None,
        note: Annotated[str | None, typer.Option(help="메모 (매수·매도 이유 등)")] = None,
    ) -> None:
        with session_scope() as session:
            acct = portfolio.get_account(session, account)
            instrument = _resolve_instrument(session, symbol, market, name)
            trade = portfolio.record_trade(
                session, acct, instrument, side, quantity, price,
                traded_at or date.today(), fee=fee, note=note,
            )
            verb = "매수" if side is Side.BUY else "매도"
            console.print(
                f"✅ {acct.name} · {instrument.display_name} {_qty(quantity)}주 "
                f"@ {_money(price, instrument.currency)} {verb} 기록"
            )
            if trade.realized_pnl is not None:
                console.print(f"   실현손익: {_signed(trade.realized_pnl, _money(trade.realized_pnl, instrument.currency))}")

    command.__doc__ = f"{'매수' if side is Side.BUY else '매도'}를 기록하고 잔고를 갱신한다."
    return command


trade_app.command("buy")(_trade(Side.BUY))
trade_app.command("sell")(_trade(Side.SELL))


@trade_app.command("list")
def trade_list(
    account: Annotated[str | None, typer.Option("--account", "-a", help="계좌 이름 또는 ID")] = None,
    limit: Annotated[int, typer.Option(help="최근 몇 건")] = 20,
) -> None:
    """최근 매매 기록을 보여준다."""
    with session_scope() as session:
        stmt = select(Trade).order_by(Trade.traded_at.desc(), Trade.id.desc()).limit(limit)
        if account:
            stmt = stmt.where(Trade.account_id == portfolio.get_account(session, account).id)
        trades = session.scalars(stmt).all()
        if not trades:
            console.print("매매 기록이 없어요.")
            return
        table = Table(Column("날짜", no_wrap=True), "계좌", "종목", "구분", "수량", "단가", "수수료", "실현손익", "메모")
        for t in trades:
            currency = t.instrument.currency
            table.add_row(
                t.traded_at.isoformat(),
                t.account.name,
                t.instrument.display_name,
                "[red]매수[/]" if t.side is Side.BUY else "[blue]매도[/]",
                _qty(t.quantity),
                _money(t.price, currency),
                _money(t.fee, currency),
                _signed(t.realized_pnl, _money(t.realized_pnl, currency)),
                t.note or "",
            )
        console.print(table)


# ── 전략 태그 ──────────────────────────────────────────


@app.command()
def tag(
    symbol: Annotated[str, typer.Argument(help="종목코드")],
    strategy: Annotated[Strategy, typer.Argument(help="long=장기, swing=스윙, short=단기")],
) -> None:
    """종목에 투자 전략 태그를 단다. AI 분석(4단계)에서 태그별로 다른 기준을 쓴다."""
    with session_scope() as session:
        instrument = portfolio.find_instrument(session, symbol)
        instrument.strategy = strategy
        console.print(f"🏷️  {instrument.display_name} → {strategy.label}")


# ── CSV 가져오기 ────────────────────────────────────────


@import_app.command("template")
def import_template() -> None:
    """잔고 CSV 템플릿을 출력한다. 예: `stock import template > holdings.csv`"""
    print(importers.TEMPLATE, end="")


@import_app.command("holdings")
def import_holdings(
    account: Annotated[str, typer.Argument(help="계좌 이름 또는 ID")],
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="CSV 파일")],
    replace: Annotated[
        bool, typer.Option("--replace", help="CSV에 없는 기존 잔고는 삭제 (계좌 전체 동기화)")
    ] = False,
) -> None:
    """CSV 파일로 보유 잔고를 가져온다."""
    rows = importers.parse_holdings_csv(file.read_text(encoding="utf-8-sig"))
    with session_scope() as session:
        acct = portfolio.get_account(session, account)
        result = importers.import_holdings(session, acct, rows, replace=replace)
        for instrument in result.imported:
            if not instrument.name:
                instrument.name = lookup_name(instrument.symbol, instrument.market)
        console.print(f"✅ {acct.name}: {len(result.imported)}개 종목을 가져왔어요.")
        if result.removed:
            names = ", ".join(i.display_name for i in result.removed)
            console.print(f"🗑️  CSV에 없어서 삭제한 종목: {names}")


# ── 증권사 API 동기화 ────────────────────────────────────


@sync_app.command("kis")
def sync_kis(
    account: Annotated[str, typer.Option("--account", "-a", help="없으면 새로 만든다")] = "한투",
    kind: Annotated[
        AccountKind, typer.Option("--kind", "-k", help="계좌를 새로 만들 때의 종류")
    ] = AccountKind.GENERAL,
) -> None:
    """한국투자증권 잔고(국내+해외)와 예수금을 가져와서 계좌를 통째로 동기화한다."""
    with session_scope() as session, console.status("한국투자증권에서 잔고를 가져오는 중..."):
        result = sync.sync_kis(session, account, kind)
    if result.created:
        console.print(f"🆕 {result.account.name} 계좌를 새로 만들었어요.")
    console.print(
        f"✅ {result.account.name}: 국내 {result.domestic}개, 해외 {result.overseas}개 종목, "
        f"예수금 {_money(result.account.cash)}원 동기화 완료"
    )
    if result.removed:
        console.print(f"🗑️  더 이상 보유하지 않아 삭제한 종목: {', '.join(result.removed)}")
    for warning in result.warnings:
        console.print(f"[yellow]⚠️ {warning}[/]")


@sync_app.command("toss")
def sync_toss(
    account: Annotated[str, typer.Option("--account", "-a", help="없으면 새로 만든다")] = "토스",
) -> None:
    """토스증권 보유 종목(평균단가 포함)과 주문 가능 현금을 가져와서 계좌를 통째로 동기화한다."""
    with session_scope() as session, console.status("토스증권에서 잔고를 가져오는 중..."):
        result = sync.sync_toss(session, account)
    if result.created:
        console.print(f"🆕 {result.account.name} 계좌를 새로 만들었어요.")
    console.print(
        f"✅ {result.account.name}: 국내 {result.domestic}개, 해외 {result.overseas}개 종목, "
        f"주문 가능 현금 원화 {_money(result.account.cash)}원, 달러 ${result.account.cash_usd or 0:,.2f} 동기화 완료"
    )
    if result.removed:
        console.print(f"🗑️  더 이상 보유하지 않아 삭제한 종목: {', '.join(result.removed)}")
    for warning in result.warnings:
        console.print(f"[yellow]⚠️ {warning}[/]")


@app.command()
def web(
    host: Annotated[str, typer.Option(help="외부 공개 시 0.0.0.0 (WEB_PASSWORD 필수)")] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
) -> None:
    """웹 대시보드를 띄운다."""
    import uvicorn

    if host not in ("127.0.0.1", "localhost") and not settings.web_password:
        raise PortfolioError("외부에 공개하려면 .env에 WEB_PASSWORD를 먼저 설정하세요.")
    console.print(f"🌐 http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    uvicorn.run("stockmate.web.app:app", host=host, port=port, log_level="warning")


@app.command()
def discover(
    ai: Annotated[bool, typer.Option("--ai/--no-ai", help="--no-ai면 후보와 패턴 성과만 보여준다 (AI 추천 없이)")] = True,
) -> None:
    """시장 전체를 훑어 종목을 발굴한다 (매주 토요일 자동 실행과 같다). 몇 분 걸린다."""
    from stockmate import discovery
    from stockmate.report import build_snapshot

    with console.status("일봉을 받고 패턴을 계산하는 중... (AI 추천까지 5~10분)"):
        with session_scope() as session:
            snapshot = build_snapshot(session, sync.price_provider())
        record = discovery.run_weekly(snapshot, use_ai=ai)
    console.print(f"조사 {record['scanned']}종목, 최근 신호 {record['signals']}개, 후보 {len(record['candidates'])}개")
    table = Table("시장", "종목", "패턴", "점수", title="후보")
    for c in record["candidates"]:
        table.add_row(c["group"], f"{c['name']} ({c['symbol']})", ", ".join(c["patterns"]), f"{c['score']:.1f}")
    console.print(table)
    if record["picks"]:
        console.print("추천: " + ", ".join(f"{p['name']}({p['symbol']})" for p in record["picks"]))


# ── 통합 현황 ──────────────────────────────────────────


def _totals_table(title: str, groups: dict[str, Totals], grand: Totals) -> Table:
    table = Table("구분", "매입금액", "평가금액", "손익", "수익률", "비중", title=title)
    for label, t in sorted(groups.items(), key=lambda kv: kv[1].value_krw, reverse=True):
        weight = t.value_krw / grand.value_krw * 100 if grand.value_krw else Decimal(0)
        table.add_row(
            label, _money(t.cost_krw), _money(t.value_krw), _pnl(t.pnl_krw), _pct(t.pnl_pct),
            f"{weight:.1f}%",
        )
    table.add_section()
    table.add_row(
        "[bold]합계[/]", _money(grand.cost_krw), f"[bold]{_money(grand.value_krw)}[/]",
        _pnl(grand.pnl_krw), _pct(grand.pnl_pct), "100%",
    )
    return table


@app.command()
def summary() -> None:
    """모든 계좌의 보유 종목을 현재가로 평가해서 보여준다 (원화 환산)."""
    with session_scope() as session:
        provider = sync.price_provider()
        with console.status("시세와 환율을 가져오는 중..."):
            positions: list[Position] = portfolio.build_positions(session, provider)
        if not positions:
            console.print("보유 종목이 없어요. `stock holding set` 또는 `stock import holdings`로 추가하세요.")
            return

        table = Table(
            "계좌", "종목", "전략", "수량", "평균단가", "현재가", "평가금액(원)", "손익(원)", "수익률",
            title="📊 보유 종목",
        )
        for p in positions:
            currency = p.instrument.currency
            table.add_row(
                p.account.name,
                p.instrument.display_name,
                p.instrument.strategy.label if p.instrument.strategy else "[dim]-[/]",
                _qty(p.quantity),
                _money(p.avg_cost, currency),
                _money(p.price, currency),
                _money(p.value_krw),
                _pnl(p.pnl_krw),
                _pct(p.pnl_pct),
            )
        console.print(table)

        grand = portfolio.totals(positions)
        by_broker = portfolio.group_totals(positions, lambda p: p.account.broker.label)
        by_market = portfolio.group_totals(positions, lambda p: p.instrument.market.value)
        console.print(_totals_table("🏦 증권사별", by_broker, grand))
        console.print(_totals_table("🌏 시장별", by_market, grand))

        if any(p.instrument.currency == "USD" for p in positions):
            console.print(f"[dim]적용 환율: 1 USD = {provider.fx_to_krw('USD'):,.2f}원[/]")
        missing = [p.instrument.display_name for p in positions if p.price is None]
        if missing:
            console.print(f"[yellow]⚠️ 현재가를 못 가져와서 합계에서 뺀 종목: {', '.join(missing)}[/]")
        console.print("[dim]시세는 무료 공개 데이터 기준이라 지연되거나 최근 종가일 수 있어요.[/]")
