"""질문 글에서 종목을 찾는다: 국내 6자리 코드, 대문자 미국 티커, 한글 종목명.

한글 종목명은 국내 상장 종목·ETF 목록(FinanceDataReader, 일주일 캐시), 자주 쓰는 미국 종목의 한글 이름,
DB에 있는 종목 이름(토스 동기화로 들어온 미국 종목의 한글 이름 포함)에서 찾는다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from collections.abc import Iterable
from pathlib import Path

from stockmate.config import settings
from stockmate.models import Market
from stockmate.portfolio import normalize_symbol

log = logging.getLogger(__name__)

Found = tuple[Market, str, str]  # (시장, 종목코드, 이름)

_KRX_CODE = re.compile(r"(?<![0-9A-Za-z])\d{6}(?![0-9A-Za-z])")
# 미국 티커: 대문자 1~5자 (BRK.B 같은 클래스주 포함). 한글 조사가 바로 붙어도 찾는다 (예: "NVDA를").
_US_TICKER = re.compile(r"(?<![A-Za-z0-9.&-])([A-Z]{1,5}(?:[.-][A-Z])?)(?![A-Za-z0-9&])")
# 티커처럼 생겼지만 투자 용어인 것 (실제 티커와 겹쳐도 질문에서는 용어로 쓰는 경우가 대부분이다)
_NOT_TICKERS = {
    "AI", "ETF", "ETN", "ISA", "IRP", "MMF", "CMA", "RP", "ELS", "DLS", "IPO", "CEO", "CFO", "USD", "KRW", "FX",
    "RSI", "MACD", "MA", "EMA", "SMA", "BB", "PER", "PBR", "PSR", "ROE", "ROA", "EPS", "BPS", "EV", "EBITDA",
    "FOMC", "CPI", "PPI", "PCE", "GDP", "FED", "ECB", "BOJ", "BOK", "SEC", "KRX", "US", "USA", "UK", "EU",
    "S", "P", "SP", "OK", "NO", "YES", "VS", "Q", "QQ", "YOY", "QOQ", "TTM", "API", "HTS", "MTS", "ATH", "TOP",
}  # fmt: skip

# 자주 쓰는 미국 종목의 한글 이름 (토스 앱 표기 위주). DB에 있는 종목은 DB 이름으로도 찾는다.
US_KOREAN_NAMES = {
    "애플": "AAPL", "마이크로소프트": "MSFT", "마소": "MSFT", "엔비디아": "NVDA", "아마존": "AMZN", "알파벳": "GOOGL",
    "구글": "GOOGL", "메타": "META", "페이스북": "META", "테슬라": "TSLA", "브로드컴": "AVGO", "넷플릭스": "NFLX",
    "AMD": "AMD", "인텔": "INTC", "퀄컴": "QCOM", "마이크론": "MU", "TSMC": "TSM", "ASML": "ASML", "암홀딩스": "ARM",
    "마벨": "MRVL", "마벨테크놀로지": "MRVL", "팔란티어": "PLTR", "오라클": "ORCL", "세일즈포스": "CRM", "어도비": "ADBE",
    "코인베이스": "COIN", "로빈후드": "HOOD", "마이크로스트래티지": "MSTR", "스트래티지": "MSTR", "써클": "CRCL",
    "버크셔": "BRK-B", "버크셔해서웨이": "BRK-B", "JP모건": "JPM", "제이피모건": "JPM", "골드만삭스": "GS",
    "뱅크오브아메리카": "BAC", "비자": "V", "마스터카드": "MA", "페이팔": "PYPL", "코카콜라": "KO", "펩시코": "PEP",
    "맥도날드": "MCD", "스타벅스": "SBUX", "나이키": "NKE", "월마트": "WMT", "코스트코": "COST", "디즈니": "DIS",
    "존슨앤존슨": "JNJ", "화이자": "PFE", "일라이릴리": "LLY", "릴리": "LLY", "노보노디스크": "NVO", "유나이티드헬스": "UNH",
    "힘스앤허스": "HIMS", "엑슨모빌": "XOM", "셰브론": "CVX", "보잉": "BA", "록히드마틴": "LMT", "헌팅턴잉걸스": "HII",
    "비스트라": "VST", "컨스텔레이션에너지": "CEG", "오클로": "OKLO", "킨더모건": "KMI", "코닝": "GLW", "네비우스": "NBIS",
    "아이렌": "IREN", "조비": "JOBY", "조비에비에이션": "JOBY", "리게티": "RGTI", "리게티컴퓨팅": "RGTI", "아이온큐": "IONQ",
    "슈퍼마이크로": "SMCI", "우버": "UBER", "에어비앤비": "ABNB", "쇼피파이": "SHOP", "스포티파이": "SPOT",
    "로켓랩": "RKLB", "소파이": "SOFI", "리얼티인컴": "O", "알리바바": "BABA",
    "슈드": "SCHD", "제피": "JEPI", "제피큐": "JEPQ",
}  # fmt: skip

# 두 글자 국내 종목명은 흔한 낱말과 겹치는 게 많아서(대상, 노을, 태양, 레몬, 진도…) 잘 알려진 것만 찾는다.
# DB에 있는 종목(보유했거나 추천받은 종목)은 두 글자여도 찾는다.
KNOWN_TWO_LETTER = {
    "기아", "두산", "한화", "농심", "효성", "한샘", "하림", "풍산", "영풍", "대웅", "보령", "루닛", "씨젠", "안랩", "웹젠",
    "한컴", "심텍", "휴젤", "파두", "툴젠", "다날", "뷰노", "쏘카", "엔켐", "원익", "한진", "한섬", "샘표", "후성", "고영",
    "테스", "바텍", "엠로", "한독", "천보", "넥센", "웅진", "삼립", "대덕", "영흥", "흥국", "신풍", "엑셈", "미코",
}  # fmt: skip

_LISTING_TTL = dt.timedelta(days=7)
_lock = threading.Lock()
_listing: dict[str, str] | None = None  # 국내 종목명 → 코드


def _listing_path() -> Path:
    return settings.db_path.parent / "krx_names.json"


def krx_names(path: Path | None = None) -> dict[str, str]:
    """국내 상장 종목·ETF 이름 → 코드. 파일에 일주일 캐시하고, 받지 못하면 예전 파일이나 빈 dict를 쓴다."""
    global _listing
    path = path or _listing_path()
    with _lock:
        if _listing is not None:
            return _listing
        cached: dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached = data["names"]
            if dt.datetime.now() - dt.datetime.fromisoformat(data["fetched_at"]) < _LISTING_TTL:
                _listing = cached
                return _listing
        except (OSError, ValueError, KeyError):
            pass
        try:
            import FinanceDataReader as fdr

            stocks = fdr.StockListing("KRX")
            names = dict(zip(stocks["Name"], stocks["Code"]))
            etfs = fdr.StockListing("ETF/KR")
            names.update(zip(etfs["Name"], etfs["Symbol"]))
            names = {str(k).strip(): str(v) for k, v in names.items() if str(k).strip()}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"fetched_at": dt.datetime.now().isoformat(), "names": names}, ensure_ascii=False),
                encoding="utf-8",
            )
            _listing = names
        except Exception as e:
            log.warning("국내 종목 목록 조회 실패: %s", e)
            _listing = cached
        return _listing


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _name_index(extra: Iterable[Found], listing: dict[str, str]) -> dict[str, tuple[Market, str]]:
    """찾을 이름 → (시장, 코드). 공백은 빼고 비교한다 ("TIGER 미국S&P500" = "TIGER미국S&P500")."""
    index: dict[str, tuple[Market, str]] = {}
    for name, code in listing.items():
        index[_compact(name)] = (Market.KRX, code)
    for name, ticker in US_KOREAN_NAMES.items():
        index[_compact(name)] = (Market.US, ticker)
    for market, symbol, name in extra:  # DB 이름이 가장 정확하므로 마지막에 덮어쓴다
        if name and name != symbol:
            index[_compact(name)] = (market, symbol)
    return index


def _allowed_two_letter(extra: Iterable[Found]) -> set[str]:
    return KNOWN_TWO_LETTER | set(US_KOREAN_NAMES) | {_compact(n) for _, s, n in extra if n and n != s}


_HANGUL = re.compile(r"[가-힣]")


def find_names(text: str, extra: Iterable[Found] = (), listing: dict[str, str] | None = None) -> list[Found]:
    """글에 나온 종목명. 긴 이름부터 찾아서 "삼성전자우" 안의 "삼성전자"처럼 겹치는 짧은 이름은 버린다.

    두 글자 한글 이름은 잘 알려진 종목(KNOWN_TWO_LETTER, 미국 종목 한글 이름, DB에 있는 종목)만,
    그리고 앞뒤가 한글이 아니거나 바로 뒤에 조사 하나가 올 때만 종목으로 본다 ("기아를"은 찾고 "기아자동차를"은 안 찾는다).
    """
    extra = list(extra)
    compact = _compact(text)
    index = _name_index(extra, krx_names() if listing is None else listing)
    two_letter = _allowed_two_letter(extra)
    taken = [False] * len(compact)
    found: list[tuple[int, Found]] = []
    for name in sorted(index, key=len, reverse=True):
        if len(name) < 2 or name not in compact:
            continue
        start = 0
        while (pos := compact.find(name, start)) >= 0:
            start = pos + 1
            end = pos + len(name)
            if any(taken[pos:end]):
                continue
            if len(name) == 2 and _HANGUL.match(name) and (name not in two_letter or not _two_letter_ok(text, name)):
                continue
            if name.isascii() and not _ascii_bounded(compact, pos, end):
                continue  # "LG"가 "LGES" 안에 있는 것처럼 영문 이름의 일부만 맞은 경우
            for i in range(pos, end):
                taken[i] = True
            market, symbol = index[name]
            found.append((pos, (market, symbol, name)))
            break
    found.sort()
    return list(dict.fromkeys(f for _, f in found))


_PARTICLES = "은는이가을를도랑과와의에만"


def _two_letter_ok(text: str, name: str) -> bool:
    """두 글자 한글 이름이 낱말로 따로 쓰였는가 (앞이 한글이 아니고, 뒤가 한글이 아니거나 조사 하나)."""
    for m in re.finditer(re.escape(name), text):
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end() : m.end() + 2]
        if _HANGUL.match(before):
            continue
        if not after or not _HANGUL.match(after[0]):
            return True
        if after[0] in _PARTICLES and (len(after) == 1 or not _HANGUL.match(after[1])):
            return True
    return False


def _ascii_bounded(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return not (before.isascii() and before.isalnum()) and not (after.isascii() and after.isalnum())


def find_symbols(text: str, extra: Iterable[Found] = (), limit: int = 5, listing: dict[str, str] | None = None) -> list[Found]:
    """질문 글에 나온 종목: 6자리 코드, 대문자 미국 티커, 한글 종목명. 같은 종목은 한 번만, 최대 limit개."""
    found: list[Found] = [(Market.KRX, code, code) for code in _KRX_CODE.findall(text)]
    names = find_names(text, extra, listing)
    found += names
    matched = {name for _, _, name in names}  # "SK하이닉스"의 "SK"처럼 종목명의 일부인 대문자는 티커로 보지 않는다
    found += [
        (Market.US, normalize_symbol(t), normalize_symbol(t))
        for t in _US_TICKER.findall(text)
        if t.replace("-", ".") not in _NOT_TICKERS and not any(t in name for name in matched)
    ]
    seen: dict[tuple[Market, str], Found] = {}
    for market, symbol, name in found:
        seen.setdefault((market, symbol), (market, symbol, name))
    return list(seen.values())[:limit]
