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
