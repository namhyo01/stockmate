"""종목 발굴용 긴 일봉(약 5년)과 조사 대상 종목 목록.

시장 전체(약 1,000종목)를 매주 훑고 과거 신호 성과를 계산하므로, 일봉을 data/candles/에 종목별로 저장해 두고
하루가 지난 것만 다시 받는다. 대시보드·AI 프롬프트용 짧은 일봉 캐시(technical.candles)와는 따로 쓴다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stockmate.config import settings
from stockmate.models import Instrument, Market
from stockmate.report import exposure_region

log = logging.getLogger(__name__)

YEARS = 5
Key = tuple[Market, str]

# 레버리지·인버스는 연금·ISA 원칙과 맞지 않고 지표도 왜곡되므로 발굴 대상에서 뺀다
_EXCLUDED_ETF_WORDS = ("레버리지", "인버스", "2X", "3X", "곱버스", "선물인버스")

# 자주 쓰는 미국 상장 ETF (지수·섹터·배당·채권·원자재)
US_ETFS = {
    "SPY": "SPDR S&P500", "VOO": "뱅가드 S&P500", "QQQ": "인베스코 QQQ", "DIA": "SPDR 다우존스", "IWM": "아이셰어즈 러셀2000",
    "VTI": "뱅가드 토탈 스톡마켓", "VT": "뱅가드 토탈 월드", "VEA": "뱅가드 선진국", "VWO": "뱅가드 신흥국",
    "SCHD": "슈왑 미국 배당", "VIG": "뱅가드 배당성장", "JEPI": "JP모건 프리미엄 인컴", "JEPQ": "JP모건 나스닥 프리미엄 인컴",
    "SMH": "반에크 반도체", "SOXX": "아이셰어즈 반도체", "XLK": "기술 섹터", "XLF": "금융 섹터", "XLE": "에너지 섹터",
    "XLV": "헬스케어 섹터", "XLI": "산업재 섹터", "XLY": "임의소비재 섹터", "XLP": "필수소비재 섹터", "XLU": "유틸리티 섹터",
    "XLB": "소재 섹터", "XLRE": "부동산 섹터", "XLC": "커뮤니케이션 섹터", "ITA": "항공우주·방산", "URA": "우라늄",
    "TLT": "미국 장기국채", "IEF": "미국 중기국채", "SHY": "미국 단기국채", "AGG": "미국 종합채권", "LQD": "투자등급 회사채",
    "GLD": "금", "SLV": "은", "IBIT": "아이셰어즈 비트코인", "EWJ": "일본", "EWY": "한국", "INDA": "인도", "MCHI": "중국",
}  # fmt: skip

# S&P500에 없는 나스닥100 종목 (S&P500 목록과 합쳐서 미국 대형주 조사 대상으로 쓴다)
NASDAQ_EXTRA = ["ASML", "ARM", "PDD", "MELI", "TEAM", "MRVL", "CRWD", "AZN", "SHOP", "MSTR", "TTD", "DDOG", "ZS", "GFS",
                "CCEP", "BIIB", "ON", "ROP", "WDAY", "MDB", "APP", "PLTR", "NBIS", "HOOD", "COIN"]  # fmt: skip

# 지수 대신 그 지수를 따르는 ETF를 쓴다 (FinanceDataReader의 KS11 지수 데이터가 늦게 갱신될 때가 있었다, 2026-10)
# 실제로 대신 살 수 있는 상품과 비교한다: 코스피 종목은 KODEX 200, 코스닥 종목은 KODEX 코스닥150
BENCHMARKS = {"kospi": (Market.KRX, "069500"), "kosdaq": (Market.KRX, "229200"), "sp500": (Market.US, "SPY"),
              "krx_us": (Market.KRX, "360750")}  # fmt: skip


@dataclass(frozen=True)
class Target:
    market: Market
    symbol: str
    name: str
    kind: str  # stock | etf
    group: str  # KOSPI / KOSDAQ / S&P500 / NASDAQ / ETF(국내) / ETF(미국)
    benchmark: str  # BENCHMARKS의 키


def universe(kospi: int = 200, kosdaq: int = 150, krx_etfs: int = 80) -> list[Target]:
    """조사 대상: 코스피 시가총액 상위, 코스닥 상위, 국내 ETF 상위, S&P500 + 나스닥100 추가분, 주요 미국 ETF."""
    import FinanceDataReader as fdr

    targets: list[Target] = []
    stocks = fdr.StockListing("KRX")
    for market_name, count in (("KOSPI", kospi), ("KOSDAQ", kosdaq)):
        top = stocks[stocks["Market"] == market_name].sort_values("Marcap", ascending=False).head(count)
        bench = "kospi" if market_name == "KOSPI" else "kosdaq"
        targets += [Target(Market.KRX, r.Code, r.Name, "stock", market_name, bench) for r in top.itertuples()]
    etfs = fdr.StockListing("ETF/KR").sort_values("MarCap", ascending=False)
    etfs = etfs[~etfs["Name"].str.contains("|".join(_EXCLUDED_ETF_WORDS), regex=True)].head(krx_etfs)
    for r in etfs.itertuples():
        region = exposure_region(Instrument(market=Market.KRX, symbol=r.Symbol, name=r.Name))
        targets.append(Target(Market.KRX, r.Symbol, r.Name, "etf", "ETF(국내)", "krx_us" if region == "미국" else "kospi"))
    sp = fdr.StockListing("S&P500")
    fixes = {"BRKB": "BRK-B", "BFB": "BF-B"}  # 목록에는 점 없이 들어 있는 클래스주
    us = {fixes.get(str(s), str(s).replace(".", "-")): str(n) for s, n in zip(sp["Symbol"], sp["Name"])}
    targets += [Target(Market.US, s, n, "stock", "S&P500", "sp500") for s, n in us.items()]
    targets += [Target(Market.US, s, s, "stock", "NASDAQ", "sp500") for s in NASDAQ_EXTRA if s not in us]
    targets += [Target(Market.US, s, n, "etf", "ETF(미국)", "sp500") for s, n in US_ETFS.items()]
    return list({(t.market, t.symbol): t for t in targets}.values())


class History:
    """종목별 일봉을 파일(pickle)로 저장한다. 오늘 이미 받은 것은 다시 받지 않는다."""

    def __init__(self, root: Path | None = None):
        self.root = root or settings.db_path.parent / "candles"

    def _path(self, key: Key) -> Path:
        market, symbol = key
        return self.root / market.value / f"{symbol.replace('/', '_')}.pkl"

    def _fresh(self, path: Path) -> bool:
        return path.exists() and dt.date.fromtimestamp(path.stat().st_mtime) == dt.date.today()

    def load(self, keys: Iterable[Key], refresh: bool = True) -> dict[Key, pd.DataFrame]:
        keys = list(dict.fromkeys(keys))
        stale = [k for k in keys if refresh and not self._fresh(self._path(k))]
        if stale:
            self._download(stale)
        frames = {}
        for key in keys:
            path = self._path(key)
            if path.exists():
                try:
                    frames[key] = pd.read_pickle(path)
                except Exception as e:
                    log.warning("일봉 파일을 읽지 못했어요 %s: %s", path, e)
        return frames

    def _save(self, key: Key, frame: pd.DataFrame | None) -> None:
        if frame is None or frame.empty:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(path)

    def _download(self, keys: list[Key]) -> None:
        start = dt.date.today() - dt.timedelta(days=365 * YEARS + 30)
        krx = [k for k in keys if k[0] is Market.KRX]
        us = [k[1] for k in keys if k[0] is Market.US]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for key, frame in zip(krx, pool.map(lambda k: _krx_daily(k[1], start), krx)):
                self._save(key, frame)
        for i in range(0, len(us), 100):  # yfinance는 여러 종목을 한 번에 받는 게 훨씬 빠르다
            for symbol, frame in _us_daily(us[i : i + 100], start).items():
                self._save((Market.US, symbol), frame)


def _clean(frame: pd.DataFrame) -> pd.DataFrame | None:
    if frame is None or frame.empty or "Close" not in frame:
        return None
    frame = frame[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).copy()
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    for column in ("Open", "High", "Low"):
        frame[column] = frame[column].where(frame[column] > 0, frame["Close"])
    return frame if not frame.empty else None


def _krx_daily(symbol: str, start: dt.date) -> pd.DataFrame | None:
    import FinanceDataReader as fdr

    try:
        frame = fdr.DataReader(symbol, start)
        if "Volume" not in frame:  # 지수(KS11)는 거래량이 없을 수 있다
            frame["Volume"] = 0
        return _clean(frame)
    except Exception as e:
        log.warning("국내 일봉 조회 실패 %s: %s", symbol, e)
        return None


def _us_daily(symbols: list[str], start: dt.date) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    try:
        data = yf.download(
            symbols, start=start.isoformat(), progress=False, auto_adjust=True, group_by="ticker", threads=True
        )
    except Exception as e:
        log.warning("미국 일봉 조회 실패: %s", e)
        return {}
    result = {}
    for symbol in symbols:
        try:
            frame = data[symbol] if len(symbols) > 1 else data
        except KeyError:
            continue
        if (cleaned := _clean(frame)) is not None:
            result[symbol] = cleaned
    return result
