import datetime as dt
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from stockmate import discovery, history, ideas, patterns, technical
from stockmate.db import make_engine
from stockmate.models import Idea, Market


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


def frame_from(closes, start="2021-01-01", volume=1_000_000):
    closes = list(map(float, closes))
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes], "Close": closes,
         "Volume": [volume] * len(closes)},
        index=pd.bdate_range(start, periods=len(closes)),
    )  # fmt: skip


def wavy(n=600, seed=1):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.02, n)))


def test_signals_do_not_look_ahead():
    full = frame_from(wavy())
    cut = full.iloc[:400]
    a = patterns.signals(cut)
    b = patterns.signals(full).iloc[:400]
    pd.testing.assert_frame_equal(a, b)  # 나중 데이터가 붙어도 과거 신호는 그대로


def test_repeated_signal_is_counted_once_per_10_days():
    f = frame_from([100 + i for i in range(400)])  # 매일 52주 신고가
    f["Volume"] = [1_000_000] * 399 + [5_000_000]
    raw = patterns.indicators(f)
    days = patterns.signals(f)["breakout_52w"]
    assert days.sum() <= 1 + (raw["vol_ratio"] >= 1.5).sum()


def test_forward_events_and_stats():
    f = frame_from([100] * 10 + [110] * 30)
    sig = pd.Series(False, index=f.index)
    sig.iloc[5] = True  # 100에 사서 20일 뒤 110
    bench = frame_from([100] * 10 + [105] * 30)
    ev = patterns.forward_events(f, sig, 20, bench)
    assert len(ev) == 1 and ev["ret"].iloc[0] == pytest.approx(0.10) and ev["bench"].iloc[0] == pytest.approx(0.05)
    s = patterns.summarize_events(ev)
    assert s.excess_mean == pytest.approx(5.0) and s.beat_rate == 100 and s.first_half_excess is None
    assert not patterns.proven(s)  # 표본이 적다


def test_proven_needs_both_halves():
    s = patterns.Stats(100, 5, 1, 55, 2.0, -0.5, 45, 3.0, 1.0)
    assert patterns.proven(s)
    assert not patterns.proven(patterns.Stats(100, 5, 1, 55, 2.0, -0.5, 45, 5.0, -1.0))


def target(symbol, market=Market.KRX, kind="stock"):
    return history.Target(market, symbol, symbol, kind, "KOSPI" if market is Market.KRX else "S&P500", "kospi")


def test_shortlist_balances_markets_and_keeps_etfs():
    cands = [discovery.Candidate(target(f"K{i}"), ["rs_pullback"], 2.0 + i) for i in range(5)]
    cands += [discovery.Candidate(target(f"U{i}", Market.US), ["rs_breakout"], 1.0 + i) for i in range(5)]
    cands += [discovery.Candidate(target(f"L{i}"), ["rs_leader"], 8.0) for i in range(3)]
    cands += [discovery.Candidate(target("E1", kind="etf"), ["long_pullback"], 0.0)]
    cands += [discovery.Candidate(target("Z"), ["oversold"], 0.0)]  # 통한 패턴이 없는 종목은 빠진다
    chosen = [c.target.symbol for c in discovery.shortlist(cands, swing=4, long=2, etfs=1)]
    assert chosen[:4] == ["K4", "U4", "K3", "U3"]  # 국내·미국 번갈아, 점수순
    assert chosen[4:6] == ["L0", "L1"] and chosen[-1] == "E1" and "Z" not in chosen


def test_due_on_saturday_morning_once():
    sat_7 = dt.datetime(2026, 10, 17, 7, 59)
    sat_9 = dt.datetime(2026, 10, 17, 9, 0)
    assert not discovery.due(sat_7, None)
    assert discovery.due(sat_9, None)
    assert not discovery.due(sat_9, dt.datetime(2026, 10, 17, 8, 5))  # 이번 주에 이미 돌렸다
    assert discovery.due(dt.datetime(2026, 10, 18, 20, 0), dt.datetime(2026, 10, 10, 8, 5))  # 일요일에 따라잡기
    assert not discovery.due(dt.datetime(2026, 10, 20, 9, 0), dt.datetime(2026, 10, 10, 8, 5))  # 화요일엔 다음 주까지 기다린다


def make_idea(**kw):
    base = dict(created_at=dt.datetime(2026, 1, 2, 10), updated_at=dt.datetime(2026, 1, 2, 10), source="weekly",
                name="테스트", symbol="005930", market=Market.KRX, status="active", horizon="swing", patterns="rs_pullback")
    base.update(kw)
    return Idea(**base)  # fmt: skip


def test_evaluate_excess_and_outcome():
    f = frame_from([100] * 5 + [100 + i for i in range(1, 40)], start="2026-01-01")
    bench = frame_from([100] * 50, start="2026-01-01")
    r = discovery.evaluate(make_idea(target=D(110)), f, bench)
    assert r["done"] and r["ret"] > 0 and r["excess"] == r["ret"]
    assert r["outcome"]["type"] == "target"
    falling = frame_from([100 - i for i in range(30)], start="2026-01-01")
    r = discovery.evaluate(make_idea(stop=D(95), target=D(120)), falling, bench)
    assert r["outcome"]["type"] == "stop" and r["excess"] < 0


def test_scorecard_groups_finished_ideas(session):
    session.add_all([
        make_idea(symbol="005930", target=D(110)),
        make_idea(symbol="000660", horizon="long", patterns="rs_leader"),
        make_idea(symbol="035420", status="replaced"),
    ])  # fmt: skip
    session.flush()
    frames = {
        (Market.KRX, "005930"): frame_from([100] * 5 + [100 + i for i in range(1, 40)], start="2026-01-01"),
        (Market.KRX, "000660"): frame_from([100 + i for i in range(60)], start="2026-01-01"),
        history.BENCHMARKS["kospi"]: frame_from([100] * 200, start="2025-12-01"),
    }
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    card = discovery.scorecard(session, source)
    assert card["done"]["count"] == 1 and card["open"]["count"] == 1  # 장기는 120일이 안 지났다, 대체된 추천은 뺀다
    assert card["by"]["패턴"]["주도주 눌림목"]["targeted"] == 1
    assert "지수 대비" in discovery.scorecard_text(session, source)


def test_apply_ideas_keeps_horizon_target_stop(session):
    added = ideas.apply_ideas(
        session,
        [{"name": "삼성전기", "symbol": "009150", "horizon": "long", "patterns": ["rs_leader", 3], "target": "250,000",
          "stop": 180000, "account": "토스"}],
        "weekly",
    )  # fmt: skip
    i = added[0]
    assert (i.horizon, i.patterns, i.target, i.stop) == ("long", "rs_leader", D(250000), D(180000))
