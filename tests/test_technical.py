import datetime as dt

import pandas as pd

from stockmate import analysis, technical
from stockmate.models import Idea, Market


def make_frame(closes, opens=None, highs=None, lows=None, volumes=None):
    n = len(closes)
    opens = opens or closes
    frame = pd.DataFrame(
        {
            "Open": opens,
            "High": highs or [max(o, c) * 1.01 for o, c in zip(opens, closes)],
            "Low": lows or [min(o, c) * 0.99 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": volumes or [1000] * n,
        },
        index=pd.bdate_range("2026-01-01", periods=n),
    )
    return frame


def test_uptrend_is_above_cloud_with_aligned_averages():
    s = technical.summarize(make_frame([100 + i for i in range(200)]))
    assert s["alignment"] == "정배열"
    assert s["ichimoku"]["position"] == "구름 위"
    assert s["ichimoku"]["chikou"] == "위"
    assert s["ichimoku"]["future_cloud"]["color"] == "양운"
    assert s["rsi"] == 100
    assert all(v < s["close"] for _, v in s["support"])
    # 신고가라 위에는 이동평균·구름이 없고 볼린저 상단과 오늘 고가뿐이다
    assert {k for k, _ in s["resistance"]} <= {"볼린저 상단", "20일 고가", "60일 고가"}


def test_downtrend_is_below_cloud():
    s = technical.summarize(make_frame([400 - i for i in range(200)]))
    assert s["alignment"] == "역배열"
    assert s["ichimoku"]["position"] == "구름 아래"
    assert s["ichimoku"]["future_cloud"]["color"] == "음운"
    assert s["rsi"] == 0


def test_short_history_is_skipped():
    assert technical.summarize(make_frame([100] * 30)) is None


def test_recent_golden_cross():
    closes = [200 - i for i in range(100)] + [100 + 5 * i for i in range(8)]
    cross = technical.summarize(make_frame(closes))["ma_cross"]["5/20"]
    assert cross["type"] == "golden" and cross["days_ago"] < 8


def test_bullish_engulfing_and_doji():
    closes = [100.0] * 30
    opens = [100.0] * 30
    opens[-2], closes[-2] = 105, 100  # 음봉
    opens[-1], closes[-1] = 99, 107  # 앞 음봉 몸통을 감싸는 양봉
    frame = make_frame(closes, opens=opens)
    names = [p["pattern"] for p in technical.candle_patterns(frame, days=1)]
    assert "상승장악형" in names

    opens[-1], closes[-1] = 100, 100.1
    frame = make_frame(closes, opens=opens, highs=[103] * 30, lows=[97] * 30)
    assert technical.candle_patterns(frame, days=1)[0]["pattern"].startswith("도지")


def test_hammer_after_decline():
    closes = [110 - i for i in range(29)] + [83.0]
    opens = closes[:-1] + [82.0]
    lows = [c * 0.99 for c in closes[:-1]] + [76.0]
    highs = [c * 1.01 for c in closes[:-1]] + [83.2]
    patterns = technical.candle_patterns(make_frame(closes, opens=opens, highs=highs, lows=lows), days=1)
    assert patterns[0]["pattern"].startswith("망치형")


def test_intraday_detection():
    seoul = dt.timezone(dt.timedelta(hours=9))
    during = dt.datetime(2026, 10, 12, 10, 0, tzinfo=seoul)
    after = dt.datetime(2026, 10, 12, 16, 0, tzinfo=seoul)
    assert technical.is_intraday(Market.KRX, dt.date(2026, 10, 12), during)
    assert not technical.is_intraday(Market.KRX, dt.date(2026, 10, 12), after)
    assert not technical.is_intraday(Market.KRX, dt.date(2026, 10, 10), during)
    # 서울 10/12 오후 4시 = 뉴욕 10/12 새벽 3시: 10/12 미국 캔들이 있다면 장 시작 전 값
    assert technical.is_intraday(Market.US, dt.date(2026, 10, 12), after)


def test_cache_reuses_and_retries_failures():
    calls = []
    frame = make_frame([100 + i for i in range(100)])

    def fetcher(market, symbol):
        calls.append(symbol)
        return frame if symbol == "005930" else None

    cache = technical.DailyCandles(fetcher, ttl=dt.timedelta(minutes=5))
    keys = [(Market.KRX, "005930"), (Market.US, "NOPE")]
    assert list(cache.get(keys)) == [(Market.KRX, "005930")]
    cache.get(keys)
    assert calls.count("005930") == 1  # 캐시에서 꺼낸다
    assert calls.count("NOPE") == 2  # 실패한 종목은 다시 시도한다


def test_chart_context_describes_and_lists_missing():
    frames = {(Market.KRX, "005930"): make_frame([100 + i for i in range(200)])}
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    text = technical.chart_context(
        [(Market.KRX, "005930", "삼성전자"), (Market.US, "NOPE", "없는 종목")], source=source
    )
    assert "#### 삼성전자 (005930, KRX)" in text
    assert "정배열" in text and "구름 위" in text
    assert "없는 종목(NOPE)" in text


def test_chart_targets_merges_holdings_and_ideas():
    snapshot = {
        "positions": [
            {"market": "KRX", "symbol": "005930", "name": "삼성전자"},
            {"market": "KRX", "symbol": "005930", "name": "삼성전자"},  # 다른 계좌의 같은 종목
            {"market": "US", "symbol": "AAPL", "name": "애플"},
        ]
    }
    active = [Idea(name="버크셔", symbol="BRK-B", market=None), Idea(name="종목코드 없음", symbol=None)]
    assert analysis.chart_targets(snapshot, active) == [
        (Market.KRX, "005930", "삼성전자"),
        (Market.US, "AAPL", "애플"),
        (Market.US, "BRK-B", "버크셔"),
    ]


def test_optional_symbols_are_dropped_silently_when_missing():
    frames = {(Market.US, "NVDA"): make_frame([100 + i for i in range(200)])}
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    text = technical.chart_context([], source=source, optional=[(Market.US, "NVDA", "NVDA"), (Market.US, "TOP", "TOP")])
    assert "#### NVDA (NVDA, US)" in text and "TOP" not in text


def test_prompt_includes_charts():
    prompt = analysis.build_prompt({"positions": []}, "기억", "#### 삼성전자 차트")
    assert "## 차트 점검" in prompt and "#### 삼성전자 차트" in prompt


def test_chart_series_shifts_cloud_forward():
    frame = make_frame([100 + i for i in range(120)])
    series = technical.chart_series(frame, Market.KRX)
    n = len(frame)
    assert len(series["dates"]) == len(series["ohlc"]) == len(series["volume"]) == n
    cloud = series["cloud"]
    assert len(cloud["dates"]) == len(cloud["a"]) == n
    # 선행스팬은 계산한 날에서 25칸 뒤에 그린다: 마지막 25개는 앞날짜(평일)
    assert cloud["dates"][0] == series["dates"][technical.SHIFT]
    assert cloud["dates"][-1] > series["dates"][-1]
    assert pd.Timestamp(cloud["dates"][-1]).weekday() < 5
    # 후행스팬: 오늘 종가를 25칸 앞 자리에 그리므로 마지막 25칸은 비어 있다
    assert series["lines"]["chikou"][-technical.SHIFT:] == [None] * technical.SHIFT
    assert series["lines"]["chikou"][-technical.SHIFT - 1] == series["ohlc"][-1][3]


def test_chart_payload_includes_summary_text():
    frames = {(Market.US, "AAPL"): make_frame([100 + i for i in range(200)])}
    source = technical.DailyCandles(lambda m, s: frames.get((m, s)))
    payload = technical.chart_payload(Market.US, "AAPL", "애플", source=source)
    assert payload["currency"] == "USD" and payload["text"].startswith("#### 애플")
    assert technical.chart_payload(Market.US, "NOPE", source=source) is None


def test_index_payload_uses_live_value_for_today():
    frame = make_frame([100 + i for i in range(200)])
    last = frame.index[-1]
    source = technical.DailyCandles(lambda m, s: frame if s == "^KS11" else None)
    live = {"key": "kospi", "label": "코스피", "value": 250.5, "at": f"{last:%Y-%m-%d}T10:37:00+09:00", "unit": ""}
    p = technical.index_payload("kospi", live, source=source)
    assert p["currency"] == "INDEX" and p["digits"] == 2
    assert p["ohlc"][-1][3] == 250.5 and p["ohlc"][-1][1] >= 250.5  # 오늘 캔들은 실시간 값으로, 고가도 함께
    assert p["text"].startswith("#### 코스피 (^KS11)")
    assert technical.index_payload("nope", None, source=source) is None


def test_index_sparks_last_month():
    frame = make_frame([100 + i for i in range(200)])
    source = technical.DailyCandles(lambda m, s: frame if s in ("^KS11", "^GSPC") else None)
    sparks = technical.index_sparks(source=source)
    assert set(sparks) == {"kospi", "sp500"} and len(sparks["kospi"]) == 22 and sparks["kospi"][-1] == 299
