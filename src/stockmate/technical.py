"""차트(일봉) 기술적 지표: 이동평균, 일목균형표, RSI, MACD, 볼린저밴드, 거래량, 캔들 패턴.

AI가 숫자 수백 개를 직접 계산하면 틀리기 쉬우므로 지표는 여기서 계산하고,
AI에게는 요약(summarize → describe)만 넘겨서 해석하게 한다.
지표는 분석용이라 Decimal이 아니라 float로 계산한다 (금액 기록에는 쓰지 않는다).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from zoneinfo import ZoneInfo

import pandas as pd

from stockmate.models import Market

log = logging.getLogger(__name__)

Key = tuple[Market, str]
Fetcher = Callable[[Market, str], pd.DataFrame | None]

# 일목균형표 기간. 선행스팬은 국내 HTS처럼 오늘을 포함해 26일째(25칸 뒤)에 그린다.
TENKAN, KIJUN, SENKOU_B, SHIFT = 9, 26, 52, 25
MIN_CANDLES = 60  # 이보다 짧으면 지표가 의미 없으므로 요약하지 않는다

# 시장별 시간대와 정규장 마감 시각 (마지막 캔들이 아직 장중인지 판단용)
_SESSION = {Market.KRX: (ZoneInfo("Asia/Seoul"), dt.time(15, 30)), Market.US: (ZoneInfo("America/New_York"), dt.time(16, 0))}


def is_intraday(market: Market, last_date: dt.date, now: dt.datetime | None = None) -> bool:
    """마지막 캔들이 오늘 장이 끝나기 전 값인가 (거래량·캔들 모양이 아직 확정되지 않았다)."""
    zone, close_at = _SESSION[market]
    local = (now or dt.datetime.now(dt.UTC)).astimezone(zone)
    return last_date == local.date() and local.time() < close_at


# ── 일봉 가져오기 ──────────────────────────────────────


def fetch_daily(market: Market, symbol: str, days: int = 550) -> pd.DataFrame | None:
    """최근 일봉 (Open, High, Low, Close, Volume, 날짜 인덱스). 실패하면 None.

    국내는 FinanceDataReader, 미국은 yfinance (수정주가 아님, 증권사 차트와 같은 값).
    """
    start = dt.date.today() - dt.timedelta(days=days)
    try:
        if market is Market.KRX:
            import FinanceDataReader as fdr

            frame = fdr.DataReader(symbol, start)
        else:
            import yfinance as yf

            frame = yf.Ticker(symbol).history(start=start.isoformat(), auto_adjust=False)
    except Exception as e:
        log.warning("일봉 조회 실패 %s: %s", symbol, e)
        return None
    if frame is None or frame.empty:
        return None
    frame = frame[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    # 거래정지일 등 시가·고가·저가가 0으로 들어온 날은 종가로 채운다
    for column in ("Open", "High", "Low"):
        frame[column] = frame[column].where(frame[column] > 0, frame["Close"])
    return frame


class DailyCandles:
    """일봉을 ttl 동안 재사용한다. 분석·질문·대시보드가 여러 스레드에서 같이 쓰므로 잠금 안에서 바꾼다."""

    def __init__(self, fetcher: Fetcher = fetch_daily, ttl: dt.timedelta = dt.timedelta(minutes=30)):
        self._fetcher = fetcher
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[Key, tuple[dt.datetime, pd.DataFrame]] = {}

    def get(self, keys: Iterable[Key]) -> dict[Key, pd.DataFrame]:
        keys = set(keys)
        now = dt.datetime.now()
        with self._lock:
            result = {k: v[1] for k, v in self._cache.items() if k in keys and now - v[0] <= self._ttl}
        missing = sorted(keys - result.keys())
        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched = dict(zip(missing, pool.map(lambda k: self._fetcher(*k), missing)))
        with self._lock:
            for key, frame in fetched.items():
                if frame is not None and not frame.empty:  # 실패한 종목은 캐시하지 않고 다음에 다시 시도한다
                    self._cache[key] = (now, frame)
                    result[key] = frame
        return result


candles = DailyCandles()


# ── 지표 계산 ──────────────────────────────────────────


def _midpoint(frame: pd.DataFrame, window: int) -> pd.Series:
    return (frame["High"].rolling(window).max() + frame["Low"].rolling(window).min()) / 2


def ichimoku(frame: pd.DataFrame) -> pd.DataFrame:
    """전환선, 기준선, 선행스팬 A·B(그린 위치 기준이 아니라 계산한 날 기준), 후행스팬 비교용 값."""
    tenkan = _midpoint(frame, TENKAN)
    kijun = _midpoint(frame, KIJUN)
    return pd.DataFrame(
        {"tenkan": tenkan, "kijun": kijun, "span_a": (tenkan + kijun) / 2, "span_b": _midpoint(frame, SENKOU_B)}
    )


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """와일더 방식 RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    total = gain + loss
    return 100 * gain / total.where(total > 0)  # 계속 오르기만 하면 100, 변동이 없으면 NaN


def macd(close: pd.Series) -> pd.DataFrame:
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": signal, "hist": line - signal})


def _last_cross(fast: pd.Series, slow: pd.Series, lookback: int = 10) -> dict[str, Any] | None:
    """최근 lookback일 안에 fast가 slow를 뚫은 마지막 교차. days_ago=0이면 오늘."""
    above = (fast > slow).dropna()
    if len(above) < 2:
        return None
    flips = above.ne(above.shift()).iloc[1:]
    flips = flips[flips]
    if flips.empty:
        return None
    position = above.index.get_loc(flips.index[-1])
    days_ago = len(above) - 1 - position
    if days_ago >= lookback:
        return None
    return {"type": "golden" if above.iloc[position] else "dead", "days_ago": days_ago}


def candle_patterns(frame: pd.DataFrame, days: int = 3) -> list[dict[str, str]]:
    """최근 며칠의 대표 캔들 패턴. 하나의 날에 여러 패턴이 붙을 수 있다."""
    o, h, l, c = frame["Open"], frame["High"], frame["Low"], frame["Close"]
    body = (c - o).abs()
    avg_body = body.rolling(20).mean()
    trend = c - c.shift(5)  # 직전 흐름: 5일 전보다 올랐으면 상승
    found = []
    for i in range(max(1, len(frame) - days), len(frame)):
        rng = h.iloc[i] - l.iloc[i]
        if rng <= 0:
            continue
        b = body.iloc[i]
        upper = h.iloc[i] - max(o.iloc[i], c.iloc[i])
        lower = min(o.iloc[i], c.iloc[i]) - l.iloc[i]
        up = c.iloc[i] >= o.iloc[i]
        before = trend.iloc[i - 1] if i >= 1 else 0
        names = []
        if b <= rng * 0.1:
            names.append("도지(매수·매도 균형, 추세 전환 신호일 수 있음)")
        elif lower >= b * 2 and upper <= b * 0.5:
            names.append("망치형(하락 끝 반등 신호)" if before < 0 else "교수형(상승 끝 경계 신호)")
        elif upper >= b * 2 and lower <= b * 0.5:
            names.append("역망치형(하락 끝 반등 시도)" if before < 0 else "유성형(상승 끝 경계 신호)")
        if pd.notna(avg_body.iloc[i]) and b >= avg_body.iloc[i] * 2 and b >= rng * 0.6:
            names.append("장대양봉" if up else "장대음봉")
        po, pc = o.iloc[i - 1], c.iloc[i - 1]
        if pc < po and up and o.iloc[i] <= pc and c.iloc[i] >= po and b > abs(pc - po):
            names.append("상승장악형")
        elif pc > po and not up and o.iloc[i] >= pc and c.iloc[i] <= po and b > abs(pc - po):
            names.append("하락장악형")
        if l.iloc[i] > h.iloc[i - 1]:
            names.append("상승 갭")
        elif h.iloc[i] < l.iloc[i - 1]:
            names.append("하락 갭")
        for name in names:
            found.append({"date": frame.index[i].strftime("%Y-%m-%d"), "pattern": name})
    return found


def _pct(a: float, b: float) -> float | None:
    return None if not b or pd.isna(a) or pd.isna(b) else round((a / b - 1) * 100, 2)


def _val(x: Any) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def summarize(frame: pd.DataFrame, market: Market | None = None) -> dict[str, Any] | None:
    """일봉으로 지표 요약을 만든다. 일봉이 너무 짧으면 None. market을 주면 장중 캔들인지 표시한다."""
    if frame is None or len(frame) < MIN_CANDLES:
        return None
    close, volume = frame["Close"], frame["Volume"]
    price = float(close.iloc[-1])
    last = len(frame) - 1

    def ago(n: int) -> float | None:
        return float(close.iloc[-1 - n]) if len(close) > n else None

    mas = {n: close.rolling(n).mean() for n in (5, 20, 60, 120, 200)}
    ma_now = {n: _val(s.iloc[-1]) for n, s in mas.items()}
    order = [ma_now[n] for n in (5, 20, 60, 120)]
    if all(v is not None for v in order):
        alignment = "정배열" if order == sorted(order, reverse=True) else "역배열" if order == sorted(order) else "혼조"
    else:
        alignment = None

    ich = ichimoku(frame)
    # 오늘 자리의 구름 = 25일 전에 계산한 선행스팬, 앞으로 25일의 구름 = 최근 25일에 계산한 선행스팬
    cloud_now = ich.iloc[last - SHIFT] if last >= SHIFT else None
    cloud_top = cloud_bottom = None
    if cloud_now is not None and pd.notna(cloud_now["span_b"]):
        cloud_top = float(max(cloud_now["span_a"], cloud_now["span_b"]))
        cloud_bottom = float(min(cloud_now["span_a"], cloud_now["span_b"]))
    position = None
    if cloud_top is not None:
        position = "구름 위" if price > cloud_top else "구름 아래" if price < cloud_bottom else "구름 안"
    future = ich.iloc[-SHIFT:]
    ahead = future["span_a"] - future["span_b"]
    twist = None
    signs = (ahead > 0).tolist()
    for i in range(1, len(signs)):
        if signs[i] != signs[i - 1]:
            twist = {"in_days": i + 1, "to": "양운" if signs[i] else "음운"}  # 오늘부터 며칠째 거래일
            break
    end = future.iloc[-1]
    chikou_ref = ago(SHIFT)

    m = macd(close)
    r = rsi(close)
    mid, std = close.rolling(20).mean(), close.rolling(20).std()
    bb_upper, bb_lower = mid + 2 * std, mid - 2 * std
    width = bb_upper - bb_lower
    year = frame.iloc[-250:]

    levels = {
        "5일선": ma_now[5], "20일선": ma_now[20], "60일선": ma_now[60], "120일선": ma_now[120],
        "200일선": ma_now[200], "기준선": _val(ich["kijun"].iloc[-1]), "전환선": _val(ich["tenkan"].iloc[-1]),
        "구름 상단": cloud_top, "구름 하단": cloud_bottom,
        "볼린저 상단": _val(bb_upper.iloc[-1]), "볼린저 하단": _val(bb_lower.iloc[-1]),
        "20일 고가": float(frame["High"].iloc[-20:].max()), "20일 저가": float(frame["Low"].iloc[-20:].min()),
        "60일 고가": float(frame["High"].iloc[-60:].max()), "60일 저가": float(frame["Low"].iloc[-60:].min()),
    }  # fmt: skip
    levels = {k: v for k, v in levels.items() if v is not None}

    return {
        "date": frame.index[-1].strftime("%Y-%m-%d"),
        "intraday": bool(market and is_intraday(market, frame.index[-1].date())),
        "close": price,
        "returns": {"1주": _pct(price, ago(5)), "1개월": _pct(price, ago(21)), "3개월": _pct(price, ago(63)),
                    "6개월": _pct(price, ago(126)), "1년": _pct(price, ago(250))},  # fmt: skip
        "high_52w": float(year["High"].max()),
        "low_52w": float(year["Low"].min()),
        "ma": ma_now,
        "alignment": alignment,
        "ma_cross": {"5/20": _last_cross(mas[5], mas[20]), "20/60": _last_cross(mas[20], mas[60])},
        "ichimoku": {
            "tenkan": _val(ich["tenkan"].iloc[-1]),
            "kijun": _val(ich["kijun"].iloc[-1]),
            "tk_cross": _last_cross(ich["tenkan"], ich["kijun"]),
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
            "position": position,
            "chikou": None if chikou_ref is None else ("위" if price > chikou_ref else "아래"),
            "chikou_ref": chikou_ref,
            "future_cloud": None if pd.isna(end["span_b"]) else {
                "top": float(max(end["span_a"], end["span_b"])),
                "bottom": float(min(end["span_a"], end["span_b"])),
                "color": "양운" if end["span_a"] > end["span_b"] else "음운",
                "twist": twist,
            },
        },
        "rsi": _val(r.iloc[-1]),
        "macd": {
            "macd": _val(m["macd"].iloc[-1]),
            "signal": _val(m["signal"].iloc[-1]),
            "hist": _val(m["hist"].iloc[-1]),
            "hist_prev": _val(m["hist"].iloc[-2]),
            "cross": _last_cross(m["macd"], m["signal"]),
        },
        "bollinger": {
            "upper": _val(bb_upper.iloc[-1]),
            "lower": _val(bb_lower.iloc[-1]),
            "pct_b": _val((close.iloc[-1] - bb_lower.iloc[-1]) / width.iloc[-1]) if width.iloc[-1] else None,
            "width_pct": _val(width.iloc[-1] / mid.iloc[-1] * 100),
            "width_rank": _val((width.iloc[-120:] <= width.iloc[-1]).mean() * 100),  # 최근 120일 중 몇 % 이하인지
        },
        "volume_ratio": _val(volume.iloc[-1] / volume.iloc[-21:-1].mean()) if volume.iloc[-21:-1].mean() else None,
        "patterns": candle_patterns(frame),
        "support": sorted(((k, v) for k, v in levels.items() if v < price), key=lambda kv: -kv[1])[:4],
        "resistance": sorted(((k, v) for k, v in levels.items() if v > price), key=lambda kv: kv[1])[:4],
    }


# ── 프롬프트용 글 ──────────────────────────────────────


def _fmt(value: float | None, currency: str) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if currency == "KRW" else f"{value:,.2f}"


def _cross_text(cross: dict[str, Any] | None, label: str) -> str | None:
    if not cross:
        return None
    when = "오늘" if cross["days_ago"] == 0 else f"{cross['days_ago']}거래일 전"
    return f"{label} {'골든크로스' if cross['type'] == 'golden' else '데드크로스'}({when})"


def describe(name: str, symbol: str, market: Market, s: dict[str, Any]) -> str:
    """요약을 프롬프트에 넣을 몇 줄의 글로 바꾼다."""
    cur = market.currency
    f = lambda v: _fmt(v, cur)  # noqa: E731
    ich, bb, mc = s["ichimoku"], s["bollinger"], s["macd"]
    returns = ", ".join(f"{k} {v:+.1f}%" for k, v in s["returns"].items() if v is not None)
    lines = [
        f"#### {name} ({symbol}, {market.value}) — {s['date']} {'장중 현재가' if s['intraday'] else '종가'} {f(s['close'])} {cur}",
        f"- 수익률: {returns} / 52주 고가 {f(s['high_52w'])}({_pct(s['close'], s['high_52w']):+.1f}%), "
        f"저가 {f(s['low_52w'])}({_pct(s['close'], s['low_52w']):+.1f}%)",
    ]
    crosses = [t for t in (_cross_text(s["ma_cross"]["5/20"], "5·20일선"), _cross_text(s["ma_cross"]["20/60"], "20·60일선")) if t]
    ma = " / ".join(f"{n}일 {f(v)}" for n, v in s["ma"].items() if v is not None)
    lines.append(f"- 이동평균: {ma} → {s['alignment'] or '판단 불가'}" + (f", {', '.join(crosses)}" if crosses else ""))

    tenkan, kijun = ich["tenkan"] or 0, ich["kijun"] or 0
    sign = ">" if tenkan > kijun else "<" if tenkan < kijun else "="
    parts = [f"전환선 {f(ich['tenkan'])} {sign} 기준선 {f(ich['kijun'])}"]
    if tk := _cross_text(ich["tk_cross"], "전환·기준선"):
        parts.append(tk)
    if ich["position"]:
        thickness = (ich["cloud_top"] - ich["cloud_bottom"]) / s["close"] * 100
        parts.append(f"주가 {ich['position']}(구름 {f(ich['cloud_bottom'])}~{f(ich['cloud_top'])}, 두께 {thickness:.1f}%)")
    if ich["chikou"]:
        parts.append(f"후행스팬이 26일 전 주가({f(ich['chikou_ref'])})보다 {ich['chikou']}")
    if fc := ich["future_cloud"]:
        twist = f", {fc['twist']['in_days']}거래일째에 {fc['twist']['to']}으로 꼬임" if fc["twist"] else ""
        parts.append(f"앞 구름(25거래일 뒤) {fc['color']} {f(fc['bottom'])}~{f(fc['top'])}{twist}")
    lines.append("- 일목균형표: " + ", ".join(parts))

    osc = []
    if s["rsi"] is not None:
        zone = " 과매수" if s["rsi"] >= 70 else " 과매도" if s["rsi"] <= 30 else ""
        osc.append(f"RSI(14) {s['rsi']:.0f}{zone}")
    if mc["hist"] is not None:
        direction = "늘어남" if mc["hist_prev"] is not None and abs(mc["hist"]) > abs(mc["hist_prev"]) else "줄어듦"
        osc.append(f"MACD 히스토그램 {'+' if mc['hist'] >= 0 else '−'}({direction})")
    if cross := _cross_text(mc["cross"], "MACD·시그널"):
        osc.append(cross)
    if bb["pct_b"] is not None:
        squeeze = ", 밴드 수축(변동성 축소)" if (bb["width_rank"] or 100) <= 15 else ""
        osc.append(f"볼린저 %b {bb['pct_b']:.2f}(밴드 {f(bb['lower'])}~{f(bb['upper'])}, 폭 {bb['width_pct']:.1f}%{squeeze})")
    if s["volume_ratio"] is not None:
        partial = "(장중이라 아직 적게 잡힘)" if s["intraday"] else ""
        osc.append(f"거래량 20일 평균의 {s['volume_ratio']:.1f}배{partial}")
    lines.append("- 보조지표: " + ", ".join(osc))

    if s["patterns"]:
        lines.append("- 최근 캔들: " + ", ".join(f"{p['date'][5:]} {p['pattern']}" for p in s["patterns"]))
    lines.append("- 아래 지지 후보: " + (", ".join(f"{k} {f(v)}" for k, v in s["support"]) or "없음(신저가 부근)"))
    lines.append("- 위 저항 후보: " + (", ".join(f"{k} {f(v)}" for k, v in s["resistance"]) or "없음(신고가 부근)"))
    return "\n".join(lines)


CHART_GUIDE = """아래는 프로그램이 일봉으로 계산한 차트 지표입니다 (숫자는 정확하니 다시 계산하지 마세요).
- 기술적 신호는 예측이 아니라 확률입니다. "오른다/내린다"가 아니라 "○○을 지키면 ~, 깨지면 ~"처럼 조건과 시나리오로 쓰세요.
- 신호 하나보다 여러 신호가 같은 방향인지(추세: 이동평균 배열·구름 위치·후행스팬, 모멘텀: MACD·RSI, 거래량 동반 여부)를 보세요. 엇갈리면 엇갈린다고 쓰세요.
- 매매 가격대(관심 가격대, 계획의 price_low·price_high, 추천의 entry)는 가능하면 지지·저항 후보에서 고르고, 어느 선인지 밝히세요.
- 종목 성격에 맞게 비중을 두세요: 장기 보유·지수 ETF는 큰 추세(60·120·200일선, 구름)와 분할매수 시점 위주, 스윙·단기는 캔들·전환선·MACD 위주.
- 차트는 뉴스·실적과 함께 판단하세요. 차트와 펀더멘털이 엇갈리면 둘 다 밝히세요."""


def chart_context(targets: Iterable[tuple[Market, str, str]], source: DailyCandles | None = None) -> str:
    """(시장, 종목코드, 이름) 목록의 차트 요약 글. 일봉을 못 가져온 종목은 그렇다고 적는다."""
    targets = list(dict.fromkeys(targets))
    if not targets:
        return "(대상 종목 없음)"
    frames = (source or candles).get((m, s) for m, s, _ in targets)
    blocks, missing = [], []
    for market, symbol, name in targets:
        summary = None
        if (frame := frames.get((market, symbol))) is not None:
            try:
                summary = summarize(frame, market)
            except Exception as e:  # 지표 하나가 깨져도 분석 전체를 막지 않는다
                log.warning("차트 지표 계산 실패 %s: %s", symbol, e)
        if summary:
            blocks.append(describe(name, symbol, market, summary))
        else:
            missing.append(f"{name}({symbol})")
    if missing:
        blocks.append(f"(일봉을 가져오지 못했거나 상장 기간이 짧아 차트 지표가 없는 종목: {', '.join(missing)})")
    return "\n\n".join(blocks)
