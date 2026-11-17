"""AI 모의투자: 추천 탭의 추천(Idea)을 AI가 말한 그대로 따랐다면 어떻게 됐을지 일봉으로 거꾸로 계산한다.

규칙 (사람이 보고 판단하는 모의 계산이고, 실제 매매는 하지 않는다):
- 추천마다 BUDGET(100만원)씩. 환율 변동은 넣지 않는다 (미국 종목도 달러 수익률 그대로).
- 매수: 추천한 다음 거래일부터. 매수 구간이 있으면 저가가 구간 위쪽 가격 이하로 내려온 첫날,
  그날 시가와 구간 위쪽 가격 중 낮은 값에 산다. 구간이 없으면 다음 거래일 시가에 산다.
  기다리는 기간(스윙 10거래일, 장기 40거래일) 안에 안 오면 '체결 안 됨', 그 전에 철회 기준을 종가로 깨면 '매수 전 철회'.
- 매도: 산 날부터 종가가 철회 기준 아래면 그 종가에 판다. 다음 날부터 고가가 목표가에 닿으면 목표가에 판다
  (시가부터 목표가 위면 시가). 같은 날 둘 다면 철회 기준이 먼저라고 본다 (보수적으로).
  스윙은 산 지 60거래일이 지나면 종가에 판다. 장기는 기준에 닿기 전까지 들고 있는다.
- AI가 같은 종목을 새로 추천해서 바뀐 추천(replaced)은 바뀐 날 종가에 판다. 사용자가 '관심 없음'으로 넘긴 것도 AI 말대로 계산한다.
- 비교: 같은 돈을 산 날 지수(국내 KODEX 200, 미국 S&P500 ETF는 TIGER 미국S&P500, 미국 종목은 SPY)에 넣었다면.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmate import ideas, technical
from stockmate.models import Idea

log = logging.getLogger(__name__)

BUDGET = 1_000_000
WAIT_DAYS = {"swing": 10, "long": 40}
HOLD_DAYS = {"swing": 60}  # 장기는 기한 없음

STATUS_LABELS = {
    "waiting": "매수 구간을 기다리는 중",
    "holding": "들고 있는 중",
    "target": "목표가에 팔았어요",
    "stop": "철회 기준에 팔았어요",
    "time": "기간이 끝나 팔았어요",
    "replaced": "새 추천으로 바뀌어 팔았어요",
    "expired": "매수 구간에 안 와서 안 샀어요",
    "cancelled": "사기 전에 철회 기준을 깼어요",
    "replaced_before": "사기 전에 새 추천으로 바뀌었어요",
}
CLOSED = ("target", "stop", "time", "replaced")


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def simulate(idea: Idea, frame: pd.DataFrame, today: dt.date | None = None) -> dict[str, Any]:
    """추천 하나를 따라 했다면: 매수일·가격, 매도일·가격·이유, 수익률."""
    horizon = idea.horizon if idea.horizon in WAIT_DAYS else "swing"
    low, high = _f(idea.entry_low), _f(idea.entry_high)
    stop, target = _f(idea.stop), _f(idea.target)
    ceiling = high or low  # 이 가격 이하로 내려오면 산다
    end_day = pd.Timestamp(idea.updated_at.date()) if idea.status == ideas.REPLACED else None
    days = frame[frame.index > pd.Timestamp(idea.created_at.date())]
    if end_day is not None:
        days = days[days.index <= end_day]
    out: dict[str, Any] = {"horizon": horizon, "entry_date": None, "entry": None, "exit_date": None, "exit": None}

    fill_i = None
    for i, (date, row) in enumerate(days.iterrows()):
        if i >= WAIT_DAYS[horizon]:
            break
        if ceiling is None:
            fill_i, price = i, float(row["Open"])
        elif row["Low"] <= ceiling:
            fill_i, price = i, min(float(row["Open"]), ceiling)
        if fill_i is not None and stop and price <= stop:  # 철회 기준 아래에서 사게 되면 AI 말로는 이미 끝난 추천
            return {**out, "status": "cancelled", "status_date": date}
        if fill_i is not None:
            out["entry_date"], out["entry"] = date, price
            break
        if stop and row["Close"] < stop:
            return {**out, "status": "cancelled", "status_date": date}
    if fill_i is None:
        if end_day is not None and len(days) < WAIT_DAYS[horizon]:
            return {**out, "status": "replaced_before", "status_date": end_day}
        waited = len(days) >= WAIT_DAYS[horizon]
        return {**out, "status": "expired" if waited else "waiting", "status_date": days.index[min(len(days), WAIT_DAYS[horizon]) - 1] if waited else None}

    held = days.iloc[fill_i:]
    for j, (date, row) in enumerate(held.iterrows()):
        if j > 0 and target and row["Open"] >= target:
            return {**out, "status": "target", "exit_date": date, "exit": float(row["Open"])}
        if stop and row["Close"] < stop:
            return {**out, "status": "stop", "exit_date": date, "exit": float(row["Close"])}
        if j > 0 and target and row["High"] >= target:
            return {**out, "status": "target", "exit_date": date, "exit": target}
        if horizon in HOLD_DAYS and j >= HOLD_DAYS[horizon]:
            return {**out, "status": "time", "exit_date": date, "exit": float(row["Close"])}
    last_date = held.index[-1]
    if end_day is not None:
        return {**out, "status": "replaced", "exit_date": last_date, "exit": float(held["Close"].iloc[-1])}
    return {**out, "status": "holding", "last": float(held["Close"].iloc[-1]), "last_date": last_date}


def _close_on(frame: pd.DataFrame | None, date: pd.Timestamp) -> float | None:
    """그날(휴장이면 그 전 거래일) 종가."""
    if frame is None:
        return None
    before = frame["Close"][frame.index <= date]
    return float(before.iloc[-1]) if not before.empty else None


def trade_row(idea: Idea, sim: dict[str, Any], bench: pd.DataFrame | None) -> dict[str, Any]:
    entry = sim["entry"]
    price_now = sim.get("exit") or sim.get("last")
    ret = (price_now / entry - 1) * 100 if entry and price_now else None
    bench_ret = None
    if entry and bench is not None:
        b0 = _close_on(bench, sim["entry_date"])
        b1 = _close_on(bench, sim["exit_date"] or sim["last_date"])
        bench_ret = (b1 / b0 - 1) * 100 if b0 and b1 else None
    day = lambda d: d.strftime("%Y-%m-%d") if d is not None else None  # noqa: E731
    return {
        "id": idea.id,
        "name": idea.name,
        "symbol": idea.symbol,
        "market": idea.market.value if idea.market else None,
        "currency": idea.market.currency if idea.market else "KRW",
        "source": idea.source,
        "idea_status": idea.status,
        "horizon": sim["horizon"],
        "created_at": idea.created_at.date().isoformat(),
        "entry_low": _f(idea.entry_low),
        "entry_high": _f(idea.entry_high),
        "stop": _f(idea.stop),
        "target": _f(idea.target),
        "status": sim["status"],
        "status_label": STATUS_LABELS[sim["status"]],
        "entry_date": day(sim["entry_date"]),
        "entry": entry,
        "exit_date": day(sim["exit_date"]) or day(sim.get("status_date")),
        "exit": sim.get("exit"),
        "price_now": price_now,
        "ret": None if ret is None else round(ret, 2),
        "pnl": None if ret is None else round(BUDGET * ret / 100),
        "bench_ret": None if bench_ret is None else round(bench_ret, 2),
        "excess": None if ret is None or bench_ret is None else round(ret - bench_ret, 2),
    }


def equity_curve(trades: list[dict[str, Any]], frames: dict[str, pd.DataFrame], benches: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    """날마다 모의 계좌의 누적 손익과, 같은 돈을 지수에 넣었을 때의 누적 손익."""
    bought = [t for t in trades if t["entry"]]
    if not bought:
        return []
    start = min(pd.Timestamp(t["entry_date"]) for t in bought)
    end = max(f.index[-1] for f in frames.values())
    dates = pd.bdate_range(start, end)
    pnl, bpnl, invested = pd.Series(0.0, index=dates), pd.Series(0.0, index=dates), pd.Series(0.0, index=dates)
    for t in bought:
        key = f"{t['market']}:{t['symbol']}"
        close = frames[key]["Close"].reindex(dates, method="ffill")
        on = dates >= pd.Timestamp(t["entry_date"])
        stop_at = pd.Timestamp(t["exit_date"]) if t["status"] in CLOSED else None
        path = BUDGET * (close / t["entry"] - 1)
        if stop_at is not None:
            path[dates >= stop_at] = t["pnl"]
        pnl += path.where(on, 0.0).fillna(0.0)
        invested += on * BUDGET
        bench = benches.get(key)
        if bench is not None:
            b = bench["Close"].reindex(dates, method="ffill")
            b0 = _close_on(bench, pd.Timestamp(t["entry_date"]))
            bpath = BUDGET * (b / b0 - 1)
            if stop_at is not None:
                bpath[dates >= stop_at] = BUDGET * (t["bench_ret"] or 0) / 100
            bpnl += bpath.where(on, 0.0).fillna(0.0)
    return [
        {"date": d.strftime("%Y-%m-%d"), "pnl": round(p), "bench": round(b), "invested": int(i)}
        for d, p, b, i in zip(dates, pnl, bpnl, invested)
    ]


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    bought = [t for t in trades if t["entry"]]
    closed = [t for t in bought if t["status"] in CLOSED]
    pnl = sum(t["pnl"] or 0 for t in bought)
    bench = sum(BUDGET * (t["bench_ret"] or 0) / 100 for t in bought if t["bench_ret"] is not None)
    invested = BUDGET * len(bought)
    count = lambda *s: sum(1 for t in trades if t["status"] in s)  # noqa: E731
    return {
        "budget": BUDGET,
        "ideas": len(trades),
        "bought": len(bought),
        "invested": invested,
        "pnl": round(pnl),
        "pct": round(pnl / invested * 100, 2) if invested else None,
        "bench_pnl": round(bench),
        "bench_pct": round(bench / invested * 100, 2) if invested else None,
        "holding": count("holding"),
        "closed": len(closed),
        "waiting": count("waiting"),
        "skipped": count("expired", "cancelled", "replaced_before"),
        "wins": sum(1 for t in closed if (t["ret"] or 0) > 0),
        "targets": count("target"),
        "stops": count("stop"),
        "beat": sum(1 for t in bought if t["excess"] is not None and t["excess"] > 0),
        "realized": round(sum(t["pnl"] or 0 for t in closed)),
    }


def run(session: Session, source: technical.DailyCandles | None = None) -> dict[str, Any]:
    from stockmate.discovery import _bench_key

    rows = list(session.scalars(select(Idea).where(Idea.symbol.is_not(None), Idea.market.is_not(None)).order_by(Idea.created_at)))
    keys = {(i.market, i.symbol) for i in rows} | {_bench_key(i) for i in rows}
    candles = (source or technical.candles).get(keys)
    trades, frames, benches, missing = [], {}, {}, []
    for idea in rows:
        frame = candles.get((idea.market, idea.symbol))
        if frame is None or frame.empty:
            missing.append(idea.name)
            continue
        try:
            sim = simulate(idea, frame)
        except Exception:
            log.exception("모의투자 계산 실패: %s", idea.name)
            missing.append(idea.name)
            continue
        bench = candles.get(_bench_key(idea))
        trades.append(trade_row(idea, sim, bench))
        key = f"{idea.market.value}:{idea.symbol}"
        frames[key] = frame
        if bench is not None:
            benches[key] = bench
    trades.sort(key=lambda t: t["created_at"], reverse=True)
    return {
        "summary": summary(trades),
        "curve": equity_curve(trades, frames, benches),
        "trades": trades,
        "missing": missing,
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
    }
