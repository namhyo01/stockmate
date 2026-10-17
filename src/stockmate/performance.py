"""기간 성과 (1주·1개월·3개월·1년): 지금 보유 종목이 지난 기간 동안 얼마나 움직였는지.

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


PERIODS = {1: "하루", 7: "1주", 30: "1개월", 91: "3개월", 365: "1년"}


def week(snapshot: dict[str, Any], days: int = 7, today: dt.date | None = None,
         source: technical.DailyCandles | None = None) -> dict[str, Any]:  # fmt: skip
    """지난 days일 성과. 기준은 days일 전 마감 평가금, 끝은 지금 평가금(스냅샷).

    기간이 길수록 '지금 가진 수량을 그때부터 들고 있었다면'에 가까워진다 (실제로 산 날·가격과 다르다).
    """
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
    calendar = pd.date_range(base_day, today - dt.timedelta(days=1), freq="D")
    total_by_day = pd.Series(0.0, index=calendar)
    fx_daily = fx.reindex(fx.index.union(calendar)).ffill().reindex(calendar) if fx is not None else None
    for p in counted:  # 날마다 다시 찾지 않고 달력에 맞춰 한 번에 채운다 (1년이면 365일)
        market = Market(p["market"])
        closes = _closes(frames[(market, p["symbol"])], market)
        price = closes.reindex(closes.index.union(calendar)).ffill().reindex(calendar)
        value = p["quantity"] * price
        if market is Market.US:
            value = value * (fx_daily if fx_daily is not None else fx_now)
        total_by_day = total_by_day + value
    daily = [{"date": d.date().isoformat(), "value": float(v)} for d, v in total_by_day.items() if pd.notna(v)]
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
        "days": days,
        "label": PERIODS.get(days, f"{days}일"),
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


def day(snapshot: dict[str, Any], minutes: dict[tuple[str, str], dict[str, Any] | None],
        benchmarks: dict[str, float | None]) -> dict[str, Any]:  # fmt: skip
    """하루 성과: 어제 종가 대비 지금 (국내는 오늘 장, 미국은 지난밤 장). 대시보드의 '오늘' 숫자와 같다.

    그래프는 종목별 1분봉(minutes: (시장, 종목) → intraday.today 결과)을 한국 시각에 맞춰 더한 하루 평가금이다.
    분봉이 시작되기 전에는 어제 종가로, 끝난 뒤에는 마지막 가격으로 본다. 미국 종목은 지금 환율로 환산한다.
    """
    fx = (snapshot.get("fx") or {}).get("USD") or 0
    positions = [p for p in snapshot.get("positions", [])
                 if p.get("quantity") and p.get("value_krw") is not None and p.get("day_pnl_krw") is not None]  # fmt: skip
    skipped = [p["name"] for p in snapshot.get("positions", []) if p.get("value_krw") is not None and p.get("day_pnl_krw") is None]
    rows = []
    for p in positions:
        base = p["value_krw"] - p["day_pnl_krw"]
        rows.append({
            "account": p["account"], "name": p["name"], "symbol": p["symbol"], "market": p["market"],
            "base": base, "now": p["value_krw"], "change": p["day_pnl_krw"],
            "pct": (p["value_krw"] / base - 1) * 100 if base else None,
        })  # fmt: skip

    def total(items: list[dict[str, Any]]) -> dict[str, Any]:
        base = sum(r["base"] for r in items)
        now = sum(r["now"] for r in items)
        return {"base": base, "now": now, "change": now - base, "pct": (now / base - 1) * 100 if base else None}

    # 분봉을 한국 시각으로 모아 하루 평가금 곡선을 만든다
    series = []  # (종목의 원화 환산 배율, 어제 종가, [(시각, 가격)])
    times: set[str] = set()
    for p in positions:
        m = minutes.get((p["market"], p["symbol"]))
        rate = fx if p["market"] == Market.US.value else 1
        prev = p.get("prev_close")
        pts = [(pt[3], pt[1]) for pt in (m or {}).get("points", []) if len(pt) > 3]
        if prev is None:
            continue
        times.update(t for t, _ in pts)
        series.append((p["quantity"] * rate, prev, pts))
    timeline = sorted(times)
    curve = []
    if timeline:
        idx = [0] * len(series)
        last = [prev for _, prev, _ in series]
        for t in timeline:
            for k, (_, _, pts) in enumerate(series):
                while idx[k] < len(pts) and pts[idx[k]][0] <= t:
                    last[k] = pts[idx[k]][1]
                    idx[k] += 1
            curve.append({"t": t, "value": sum(mult * price for (mult, _, _), price in zip(series, last))})
        step = max(1, len(curve) // 400)  # 그래프에는 400점 정도면 충분하다
        curve = curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else [])
    now_total = sum(r["now"] for r in rows)
    curve.insert(0, {"t": None, "value": sum(r["base"] for r in rows), "base": True})
    curve.append({"t": dt.datetime.now().isoformat(timespec="minutes"), "value": now_total, "live": True})

    accounts: dict[str, list] = {}
    for r in rows:
        accounts.setdefault(r["account"], []).append(r)
    ordered = sorted(rows, key=lambda r: r["change"])
    return {
        "days": 1,
        "label": PERIODS[1],
        "from": None,
        "to": dt.date.today().isoformat(),
        "total": total(rows),
        "accounts": [{"account": a, **total(items)} for a, items in accounts.items()],
        "benchmarks": benchmarks,
        "daily": curve,
        "gainers": [r for r in reversed(ordered) if r["change"] > 0][:5],
        "losers": [r for r in ordered if r["change"] < 0][:5],
        "skipped": skipped,
    }
