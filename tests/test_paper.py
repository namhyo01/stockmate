import datetime as dt
from decimal import Decimal

import pandas as pd

from stockmate import paper
from stockmate.models import Idea, Market


def frame(rows, start="2026-10-05"):
    """rows: [(open, high, low, close)] 평일마다."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


def idea(**kw):
    base = dict(id=1, name="테스트", symbol="AAA", market=Market.US, source="weekly", status="active", horizon="swing",
                created_at=dt.datetime(2026, 10, 2, 9), updated_at=dt.datetime(2026, 10, 2, 9))  # fmt: skip
    base.update({k: Decimal(str(v)) if isinstance(v, (int, float)) else v for k, v in kw.items()})
    return Idea(**base)


def test_buys_when_price_comes_into_zone_and_sells_at_target():
    f = frame([(110, 111, 105, 106), (104, 105, 99, 100), (101, 109, 100, 108), (108, 121, 107, 118)])
    sim = paper.simulate(idea(entry_low=95, entry_high=102, stop=90, target=120), f)
    assert sim["status"] == "target" and sim["entry"] == 102 and sim["exit"] == 120  # 시가 104 > 구간 위 102 → 102에 산다
    assert sim["entry_date"] == pd.Timestamp("2026-10-06") and sim["exit_date"] == pd.Timestamp("2026-10-08")


def test_gap_down_buys_at_open_and_stop_by_close():
    f = frame([(98, 99, 96, 97), (95, 96, 88, 89)])
    sim = paper.simulate(idea(entry_high=102, stop=90), f)
    assert sim["entry"] == 98 and sim["status"] == "stop" and sim["exit"] == 89


def test_same_day_target_and_stop_counts_as_stop():
    f = frame([(100, 101, 99, 100), (100, 125, 85, 86)])
    assert paper.simulate(idea(stop=90, target=120), f)["status"] == "stop"


def test_never_reaches_zone_expires_after_wait_days_or_still_waiting():
    f = frame([(110, 112, 108, 110)] * 12)
    sim = paper.simulate(idea(entry_high=100), f)
    assert sim["status"] == "expired" and sim["entry"] is None
    assert paper.simulate(idea(entry_high=100), frame([(110, 112, 108, 110)] * 3))["status"] == "waiting"
    assert paper.simulate(idea(entry_high=100, horizon="long"), f)["status"] == "waiting"  # 장기는 40거래일까지 기다린다


def test_stop_broken_before_buying_cancels():
    f = frame([(110, 111, 104, 105), (101, 102, 99, 100)])
    assert paper.simulate(idea(entry_high=100, stop=106), f)["status"] == "cancelled"


def test_swing_time_exit_and_long_keeps_holding():
    f = frame([(100, 101, 99, 100)] + [(100, 103, 99, 101)] * 70)
    sim = paper.simulate(idea(), f)
    assert sim["status"] == "time" and sim["exit"] == 101
    held = paper.simulate(idea(horizon="long"), f)
    assert held["status"] == "holding" and held["last"] == 101


def test_replaced_idea_sells_on_replacement_day():
    f = frame([(100, 101, 99, 100), (100, 103, 99, 102), (102, 104, 101, 103), (103, 105, 102, 104)])
    sim = paper.simulate(idea(status="replaced", updated_at=dt.datetime(2026, 10, 6, 12)), f)
    assert sim["status"] == "replaced" and sim["exit"] == 102


def test_trade_row_and_summary_against_bench():
    f = frame([(100, 101, 99, 100), (100, 111, 99, 110)])
    bench = frame([(50, 50, 50, 50), (50, 51, 50, 51)])
    i = idea(target=110)
    row = paper.trade_row(i, paper.simulate(i, f), bench)
    assert row["ret"] == 10.0 and row["pnl"] == 100_000 and row["bench_ret"] == 2.0 and row["excess"] == 8.0
    s = paper.summary([row])
    assert s["invested"] == 1_000_000 and s["pct"] == 10.0 and s["bench_pct"] == 2.0 and s["wins"] == 1 and s["targets"] == 1
    curve = paper.equity_curve([row], {"US:AAA": f}, {"US:AAA": bench})
    assert curve[-1]["pnl"] == 100_000 and curve[-1]["bench"] == 20_000 and curve[0]["invested"] == 1_000_000
