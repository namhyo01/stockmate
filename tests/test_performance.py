import datetime as dt

import pandas as pd

from stockmate import performance, technical
from stockmate.models import Market


def frame(closes, start):
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [1] * len(closes)},
                        index=pd.date_range(start, periods=len(closes)))  # fmt: skip


def test_week_uses_current_quantity_and_shifts_us_by_a_day():
    today = dt.date(2026, 10, 15)
    frames = {
        (Market.KRX, "005930"): frame([100.0] * 8 + [80.0], "2026-10-07"),  # 10/7~10/15, 10/8 종가 100
        (Market.US, "AAPL"): frame([10.0, 10.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 12.0], "2026-10-06"),
        performance.FX_KEY: frame([1000.0] * 12, "2026-10-04"),
    }
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    snapshot = {"fx": {"USD": 1000.0}, "positions": [
        {"account": "토스", "name": "삼성전자", "symbol": "005930", "market": "KRX", "quantity": 10, "value_krw": 800.0},
        {"account": "토스", "name": "애플", "symbol": "AAPL", "market": "US", "quantity": 1, "value_krw": 12000.0},
    ]}  # fmt: skip
    w = performance.week(snapshot, today=today, source=source)
    # 기준: 10/8 마감. 국내 100×10=1000, 미국은 한국 10/8에 미국 10/7 종가(10)×1000
    assert w["total"]["base"] == 1000 + 10000 and w["total"]["now"] == 12800
    assert [r["name"] for r in w["losers"]] == ["삼성전자"] and [r["name"] for r in w["gainers"]] == ["애플"]
    assert w["daily"][0]["date"] == "2026-10-08" and w["daily"][-1]["live"]
    assert w["accounts"][0]["account"] == "토스" and w["skipped"] == []


def test_longer_period_and_listing_after_base_is_skipped():
    today = dt.date(2026, 10, 15)
    frames = {
        (Market.KRX, "005930"): frame([100.0 + i for i in range(60)], "2026-08-17"),
        (Market.KRX, "0091C0"): frame([10.0] * 5, "2026-10-11"),  # 최근 상장: 30일 전 가격이 없다
        performance.FX_KEY: frame([1000.0] * 60, "2026-08-17"),
    }
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    snapshot = {"fx": {"USD": 1000.0}, "positions": [
        {"account": "토스", "name": "삼성전자", "symbol": "005930", "market": "KRX", "quantity": 1, "value_krw": 159.0},
        {"account": "한투", "name": "새 ETF", "symbol": "0091C0", "market": "KRX", "quantity": 1, "value_krw": 10.0},
    ]}  # fmt: skip
    w = performance.week(snapshot, days=30, today=today, source=source)
    assert w["label"] == "1개월" and w["skipped"] == ["새 ETF"]
    assert len(w["daily"]) == 31 and w["daily"][0]["date"] == "2026-09-15"
    assert w["total"]["base"] == 100 + 29  # 9/15 종가


def test_day_matches_dashboard_day_pnl_and_builds_curve():
    snapshot = {"fx": {"USD": 1000.0}, "positions": [
        {"account": "토스", "name": "삼성전자", "symbol": "005930", "market": "KRX", "quantity": 10, "value_krw": 2000.0,
         "day_pnl_krw": -200.0, "prev_close": 220.0},
        {"account": "토스", "name": "애플", "symbol": "AAPL", "market": "US", "quantity": 1, "value_krw": 11000.0,
         "day_pnl_krw": 1000.0, "prev_close": 10.0},
        {"account": "한투", "name": "모름", "symbol": "X", "market": "KRX", "quantity": 1, "value_krw": 5.0, "day_pnl_krw": None},
    ]}  # fmt: skip
    minutes = {
        ("KRX", "005930"): {"points": [["09:00", 210.0, 1, "2026-10-16T09:00+09:00"], ["15:30", 200.0, 1, "2026-10-16T15:30+09:00"]]},
        ("US", "AAPL"): {"points": [["22:30", 10.5, 1, "2026-10-16T22:30+09:00"], ["04:59", 11.0, 1, "2026-10-17T04:59+09:00"]]},
    }
    d = performance.day(snapshot, minutes, {"kospi": -1.0, "sp500": 0.5})
    assert d["total"]["change"] == 800 and d["total"]["base"] == 2200 + 10000
    assert d["skipped"] == ["모름"]
    values = [p["value"] for p in d["daily"]]
    # 어제 종가 → 국내 09:00(애플은 아직 어제 종가) → 15:30 → 미국 22:30 → 04:59 → 지금
    assert values == [12200, 2100 + 10000, 2000 + 10000, 2000 + 10500, 2000 + 11000, 13000]
    assert [r["name"] for r in d["gainers"]] == ["애플"] and d["daily"][0]["base"]
