"""종목 발굴용 차트 패턴과 백테스트.

패턴마다 '그날 종가까지의 데이터만 보고' 신호를 낸다 (미래 데이터를 쓰지 않는다).
같은 함수로 오늘 신호(발굴)와 과거 신호(백테스트)를 함께 계산하므로, AI에게 "이 신호는 과거에 얼마나 맞았나"를
감이 아니라 숫자로 줄 수 있다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from stockmate.technical import SHIFT, ichimoku, rsi

Signal = Callable[[dict[str, pd.Series]], pd.Series]


@dataclass(frozen=True)
class Pattern:
    key: str
    label: str
    horizon: str  # swing | long
    days: int  # 성과를 재는 기간 (거래일)
    description: str
    signal: Signal


def indicators(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """패턴들이 같이 쓰는 지표. 모두 그날까지의 값만 쓴다."""
    o, h, l, c, v = (frame[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    ich = ichimoku(frame)
    span_a, span_b = ich["span_a"].shift(SHIFT), ich["span_b"].shift(SHIFT)  # 오늘 자리의 구름
    mid, std = c.rolling(20).mean(), c.rolling(20).std()
    width = (4 * std) / mid
    avg_vol = v.shift(1).rolling(20).mean()
    return {
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "ma20": c.rolling(20).mean(), "ma60": c.rolling(60).mean(), "ma120": c.rolling(120).mean(),
        "ma200": c.rolling(200).mean(),
        "tenkan": ich["tenkan"], "kijun": ich["kijun"],
        "cloud_top": pd.concat([span_a, span_b], axis=1).max(axis=1, skipna=False),
        "cloud_bottom": pd.concat([span_a, span_b], axis=1).min(axis=1, skipna=False),
        "rsi": rsi(c),
        "bb_upper": mid + 2 * std,
        "bb_width_rank": width.rolling(120).rank(pct=True),
        "vol_ratio": v / avg_vol.where(avg_vol > 0),
        "high_52w_prev": h.shift(1).rolling(250).max(),
        "ret_126": c / c.shift(126) - 1,
        "value_20": (c * v).rolling(20).mean(),  # 하루 평균 거래대금 (유동성 거르기용)
    }  # fmt: skip


def _pullback(x):
    """상승 추세(20>60>120일선, 구름 위)에서 기준선까지 눌린 날."""
    up = (x["ma20"] > x["ma60"]) & (x["ma60"] > x["ma120"]) & (x["close"] > x["cloud_top"])
    return up & (x["low"] <= x["kijun"] * 1.01) & (x["close"] >= x["kijun"] * 0.99) & (x["rsi"] < 55)


def _breakout_52w(x):
    """종가가 52주 고가를 거래량 1.5배 이상으로 넘은 날."""
    return (x["close"] > x["high_52w_prev"]) & (x["vol_ratio"] >= 1.5)


def _cloud_break(x):
    """종가가 구름 위로 올라선 첫날 (전환선 > 기준선, 거래량 1.5배 이상)."""
    above = x["close"] > x["cloud_top"]
    return above & ~above.shift(1, fill_value=False) & (x["tenkan"] > x["kijun"]) & (x["vol_ratio"] >= 1.5)


def _squeeze_break(x):
    """볼린저밴드가 최근 120일 중 가장 좁은 10% 안에 있다가 상단을 뚫은 날 (60일선 위)."""
    return (x["bb_width_rank"].shift(1) <= 0.1) & (x["close"] > x["bb_upper"]) & (x["close"] > x["ma60"])


def _oversold_in_uptrend(x):
    """200일선 위(장기 상승)에서 RSI가 30 아래로 갔다가 다시 30 위로 올라온 날."""
    return (x["close"] > x["ma200"]) & (x["rsi"].shift(1) < 30) & (x["rsi"] >= 30)


def _long_pullback(x):
    """200일선이 오르는 중에 주가가 200일선 근처(−3%~+2%)까지 내려온 날."""
    rising = x["ma200"] > x["ma200"].shift(20)
    return rising & (x["low"] <= x["ma200"] * 1.02) & (x["close"] >= x["ma200"] * 0.97)


def _golden_cross(x):
    """60일선이 200일선을 위로 뚫은 날."""
    above = x["ma60"] > x["ma200"]
    return above & ~above.shift(1, fill_value=False) & x["ma200"].notna()


def _leader(x, bench_ret: pd.Series | None) -> pd.Series:
    """주도주: 6개월 수익률이 지수보다 20%p 넘게 높고, 200일선 위, 52주 고가에서 25% 안쪽.

    마지막 조건이 없으면 지수가 더 많이 빠진 덕에 '상대적으로' 강해 보이는 급락주가 섞인다.
    """
    excess = x["ret_126"] - (bench_ret if bench_ret is not None else 0)
    return (excess >= 0.2) & (x["close"] > x["ma200"]) & (x["close"] >= x["high_52w_prev"] * 0.75)


def _rs_leader(x, bench_ret=None):
    """주도주가 60일 고가를 새로 쓴 날."""
    return _leader(x, bench_ret) & (x["close"] >= x["close"].rolling(60).max())


def _rs_pullback(x, bench_ret=None):
    """주도주가 기준선까지 눌린 날."""
    return _leader(x, bench_ret) & (x["low"] <= x["kijun"] * 1.01) & (x["close"] >= x["kijun"] * 0.99)


def _rs_breakout(x, bench_ret=None):
    """주도주가 거래량을 동반해 52주 고가를 넘은 날."""
    return _leader(x, bench_ret) & _breakout_52w(x)


PATTERNS = [
    Pattern("pullback", "추세 눌림목", "swing", 20, "상승 추세(이평 정배열·구름 위)에서 기준선까지 눌린 자리", _pullback),
    Pattern("breakout_52w", "52주 신고가 돌파", "swing", 20, "거래량을 동반해 52주 고가를 넘은 자리", _breakout_52w),
    Pattern("cloud_break", "구름 돌파", "swing", 20, "거래량을 동반해 일목 구름 위로 올라선 첫날", _cloud_break),
    Pattern("squeeze", "변동성 수축 후 돌파", "swing", 20, "볼린저밴드가 바짝 좁아졌다가 상단을 뚫은 자리", _squeeze_break),
    Pattern("oversold", "상승 추세 속 과매도 반등", "swing", 20, "200일선 위에서 RSI 30 아래로 갔다가 회복한 자리", _oversold_in_uptrend),
    Pattern("long_pullback", "200일선 눌림", "long", 120, "200일선이 오르는 중에 200일선까지 내려온 자리", _long_pullback),
    Pattern("golden_cross", "장기 골든크로스", "long", 120, "60일선이 200일선을 위로 뚫은 자리", _golden_cross),
    Pattern("rs_leader", "상대강도 주도주", "long", 120, "6개월간 지수보다 20%p 넘게 강하고(52주 고가 25% 안쪽) 60일 고가를 새로 쓴 종목", _rs_leader),
    Pattern("rs_pullback", "주도주 눌림목", "swing", 60, "6개월간 지수보다 20%p 넘게 강한 종목이 기준선까지 눌린 자리", _rs_pullback),
    Pattern("rs_breakout", "주도주 신고가 돌파", "swing", 20, "6개월간 지수보다 20%p 넘게 강한 종목이 거래량을 동반해 52주 고가를 넘은 자리", _rs_breakout),
]  # fmt: skip
_NEEDS_BENCH = {"rs_leader", "rs_pullback", "rs_breakout"}
BY_KEY = {p.key: p for p in PATTERNS}


def signals(frame: pd.DataFrame, bench: pd.DataFrame | None = None) -> pd.DataFrame:
    """패턴별 신호 (True/False, 날짜 × 패턴). 같은 신호가 10거래일 안에 다시 나면 첫날만 남긴다."""
    x = indicators(frame)
    bench_ret = None
    if bench is not None and not bench.empty:
        bench_ret = (bench["Close"] / bench["Close"].shift(126) - 1).reindex(frame.index, method="ffill")
    out = {}
    for p in PATTERNS:
        raw = (p.signal(x, bench_ret) if p.key in _NEEDS_BENCH else p.signal(x)).fillna(False).astype(bool)
        recent = raw.shift(1, fill_value=False).astype(int).rolling(10, min_periods=1).max().astype(bool)
        out[p.key] = raw & ~recent
    return pd.DataFrame(out, index=frame.index)


@dataclass
class Stats:
    count: int
    mean: float  # 평균 수익률 (%)
    median: float
    win_rate: float  # 오른 비율 (%)
    excess_mean: float  # 지수 대비 평균 초과 수익 (%p)
    excess_median: float  # 지수 대비 초과 수익의 중간값 (평균만 높고 중간값이 낮으면 소수의 큰 승자가 끌어올린 것)
    beat_rate: float  # 지수를 이긴 비율 (%)
    first_half_excess: float | None  # 기간 앞·뒤 절반의 초과 수익 (한쪽에서만 좋았는지 보려고)
    second_half_excess: float | None

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def forward_events(
    frame: pd.DataFrame, sig: pd.Series, days: int, bench: pd.DataFrame | None
) -> pd.DataFrame:
    """신호가 난 날 종가에 사서 days 거래일 뒤 종가에 판 수익률과 같은 기간 지수 수익률."""
    close = frame["Close"]
    fwd = close.shift(-days) / close - 1
    if bench is not None and not bench.empty:
        b = bench["Close"].reindex(frame.index, method="ffill")
        bench_fwd = b.shift(-days) / b - 1
    else:
        bench_fwd = pd.Series(0.0, index=frame.index)
    events = pd.DataFrame({"ret": fwd, "bench": bench_fwd})[sig.reindex(frame.index, fill_value=False)]
    return events.dropna()


def summarize_events(events: pd.DataFrame) -> Stats | None:
    if events.empty:
        return None
    events = events.sort_index()
    excess = events["ret"] - events["bench"]
    half = len(events) // 2
    first = excess.iloc[:half].mean() * 100 if half >= 10 else None
    second = excess.iloc[half:].mean() * 100 if len(events) - half >= 10 else None
    return Stats(
        count=len(events),
        mean=float(events["ret"].mean() * 100),
        median=float(events["ret"].median() * 100),
        win_rate=float((events["ret"] > 0).mean() * 100),
        excess_mean=float(excess.mean() * 100),
        excess_median=float(excess.median() * 100),
        beat_rate=float((excess > 0).mean() * 100),
        first_half_excess=None if first is None else float(first),
        second_half_excess=None if second is None else float(second),
    )


def proven(stats: Stats | None, min_count: int = 30) -> bool:
    """과거에 꾸준히 통했던 패턴인가: 표본이 충분하고, 기간 앞·뒤 절반 모두 평균이 지수보다 나았다.

    이긴 비율(beat_rate)은 조건에 넣지 않는다. 추세 추종 신호는 대부분 50%를 밑돌고(작게 지고 크게 이긴다),
    그 점은 AI에게 숫자로 그대로 알려준다.
    """
    if stats is None or stats.count < min_count or stats.excess_mean <= 0:
        return False
    halves = [stats.first_half_excess, stats.second_half_excess]
    return all(h is not None and h > 0 for h in halves)
