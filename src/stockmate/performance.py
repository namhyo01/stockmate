"""기간 성과 (지금은 일주일): 지금 보유 종목이 지난 기간 동안 얼마나 움직였는지.

날마다의 자산 기록이 아직 없으므로 '지금 보유 수량 × 그날 종가 × 그날 환율'로 거꾸로 계산한다.
그래서 그 기간에 사거나 판 종목, 넣거나 뺀 현금은 반영되지 않는다 (화면에도 밝힌다). 현금은 빼고 주식 평가금만 본다.
미국 종목은 한국 날짜로 하루 늦게 맞춘다 (한국 d일 저녁의 평가에는 미국 d-1일 종가가 쓰인다).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from stockmate import history, technical
from stockmate.models import Market

FX_KEY = (Market.US, "KRW=X")  # 원/달러 일봉 (yfinance)


def _asof(series: pd.Series, day: dt.date) -> float | None:
    """day(포함) 이전 마지막 값."""
    s = series[series.index <= pd.Timestamp(day)]
    return float(s.iloc[-1]) if not s.empty else None


def _closes(frame: pd.DataFrame, market: Market) -> pd.Series:
    closes = frame["Close"]
    if market is Market.US:  # 미국 d일 종가는 한국 d+1일 아침에 나온다
        closes = closes.copy()
        closes.index = closes.index + pd.Timedelta(days=1)
    return closes


def week(snapshot: dict[str, Any], days: int = 7, today: dt.date | None = None,
         source: technical.DailyCandles | None = None) -> dict[str, Any]:  # fmt: skip
    """지난 days일 성과. 기준은 days일 전 마감 평가금, 끝은 지금 평가금(스냅샷)."""
    today = today or dt.date.today()
    base_day = today - dt.timedelta(days=days)
    positions = [p for p in snapshot.get("positions", []) if p.get("quantity") and p.get("value_krw") is not None]
    keys = {(Market(p["market"]), p["symbol"]) for p in positions}
    bench_keys = {"kospi": history.BENCHMARKS["kospi"], "sp500": history.BENCHMARKS["sp500"]}
    frames = (source or technical.candles).get(keys | set(bench_keys.values()) | {FX_KEY})
    fx = frames[FX_KEY]["Close"] if FX_KEY in frames else None
    fx_now = (snapshot.get("fx") or {}).get("USD")

    def value_on(p: dict[str, Any], day: dt.date) -> float | None:
        market = Market(p["market"])
        frame = frames.get((market, p["symbol"]))
        if frame is None:
            return None
        price = _asof(_closes(frame, market), day)
        if price is None:
            return None
        if market is Market.US:
            rate = _asof(fx, day) if fx is not None else fx_now
            return None if rate is None else p["quantity"] * price * rate
        return p["quantity"] * price

    rows, skipped = [], []
    for p in positions:
        base = value_on(p, base_day)
        if base is None:
            skipped.append(p["name"])
            continue
        rows.append({
            "account": p["account"], "name": p["name"], "symbol": p["symbol"], "market": p["market"],
            "base": base, "now": p["value_krw"], "change": p["value_krw"] - base,
            "pct": (p["value_krw"] / base - 1) * 100 if base else None,
        })  # fmt: skip

    def total(items: list[dict[str, Any]]) -> dict[str, Any]:
        base = sum(r["base"] for r in items)
        now = sum(r["now"] for r in items)
        return {"base": base, "now": now, "change": now - base, "pct": (now / base - 1) * 100 if base else None}

    # 날마다의 주식 평가금 (그래프용). 오늘은 지금 평가금, 나머지는 그날 마감 기준
    included = {(r["account"], r["symbol"]) for r in rows}
    counted = [p for p in positions if (p["account"], p["symbol"]) in included]
    daily = []
    for i in range(days, 0, -1):
        day = today - dt.timedelta(days=i)
        values = [value_on(p, day) for p in counted]
        if all(v is not None for v in values):
            daily.append({"date": day.isoformat(), "value": sum(values)})
    daily.append({"date": today.isoformat(), "value": sum(r["now"] for r in rows), "live": True})

    benches = {}
    for name, key in bench_keys.items():
        frame = frames.get(key)
        if frame is not None:
            closes = _closes(frame, key[0])
            a, b = _asof(closes, base_day), _asof(closes, today)
            benches[name] = (b / a - 1) * 100 if a and b else None

    accounts: dict[str, list] = {}
    for r in rows:
        accounts.setdefault(r["account"], []).append(r)
    ordered = sorted(rows, key=lambda r: r["change"])
    return {
        "from": base_day.isoformat(),
        "to": today.isoformat(),
        "total": total(rows),
        "accounts": [{"account": a, **total(items)} for a, items in accounts.items()],
        "benchmarks": benches,
        "daily": daily,
        "gainers": [r for r in reversed(ordered) if r["change"] > 0][:5],
        "losers": [r for r in ordered if r["change"] < 0][:5],
        "skipped": skipped,
    }
