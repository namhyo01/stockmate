from stockmate import symbols
from stockmate.models import Market

LISTING = {"삼성전자": "005930", "삼성전자우": "005935", "SK하이닉스": "000660", "기아": "000270", "대상": "001680",
           "LG": "003550", "두산에너빌리티": "034020", "TIGER 미국S&P500": "360750"}  # fmt: skip


def find(text, extra=()):
    return symbols.find_symbols(text, extra, listing=LISTING)


def test_codes_and_us_tickers():
    found = find("NVDA랑 BRK.B, 000660 중에 ETF 말고 뭐가 나아? RSI랑 FOMC도 봐줘. S&P500은?")
    assert found == [(Market.KRX, "000660", "000660"), (Market.US, "NVDA", "NVDA"), (Market.US, "BRK-B", "BRK-B")]


def test_korean_names_longest_first():
    assert find("삼성전자우 사도 될까?") == [(Market.KRX, "005935", "삼성전자우")]
    assert find("삼성 전자랑 두산에너빌리티 비교") == [
        (Market.KRX, "005930", "삼성전자"), (Market.KRX, "034020", "두산에너빌리티")
    ]  # fmt: skip
    # 띄어쓰기를 빼고 비교한다
    assert find("TIGER미국S&P500 더 살까") == [(Market.KRX, "360750", "TIGER미국S&P500")]


def test_us_korean_names_and_db_names():
    assert find("엔비디아랑 테슬라 어때?") == [(Market.US, "NVDA", "엔비디아"), (Market.US, "TSLA", "테슬라")]
    extra = [(Market.US, "HII", "헌팅턴 잉걸스")]
    assert find("헌팅턴 잉걸스 더 살까", extra) == [(Market.US, "HII", "헌팅턴잉걸스")]


def test_two_letter_names_only_as_words():
    assert find("기아 지금 어때?") == [(Market.KRX, "000270", "기아")]
    assert find("기아를 살까") == [(Market.KRX, "000270", "기아")]
    assert find("분석 대상 종목을 알려줘") == []
    assert find("기아자동차 말고") == []  # 더 긴 낱말의 일부
    # DB에 있는 종목은 잘 알려진 목록에 없어도 찾는다
    assert find("대상 더 살까", [(Market.KRX, "001680", "대상")]) == [(Market.KRX, "001680", "대상")]


def test_ticker_inside_korean_name_is_not_a_ticker():
    assert find("SK하이닉스 실적") == [(Market.KRX, "000660", "SK하이닉스")]
    assert find("LG 어때") == [(Market.KRX, "003550", "LG")]
    assert find("LGES 어때") == [(Market.US, "LGES", "LGES")]


def test_limit():
    assert len(find("AAPL MSFT NVDA AMZN GOOGL META TSLA")) == 5


US = {"NVDA": "NVIDIA Corporation", "NVO": "Novo Nordisk", "AAPL": "Apple Inc.", "APP": "AppLovin"}


def test_search_ranks_exact_ticker_then_prefix():
    rows = symbols.search("nvd", krx=LISTING, us=US)
    assert rows[0]["symbol"] == "NVDA"
    rows = symbols.search("APP", krx=LISTING, us=US)
    assert [r["symbol"] for r in rows[:2]] == ["APP", "AAPL"]  # 티커가 같은 게 먼저, 이름이 시작하는 게 다음


def test_search_korean_names_and_direct_lookup():
    assert symbols.search("삼성", krx=LISTING, us=US)[0]["symbol"] == "005930"  # 짧은 이름이 먼저
    assert symbols.search("엔비디아", krx=LISTING, us=US)[0] == {"market": "US", "symbol": "NVDA", "name": "NVIDIA Corporation"}
    assert symbols.search("PLTR", krx=LISTING, us=US)[-1] == {"market": "US", "symbol": "PLTR", "name": "PLTR 직접 조회"}
    assert symbols.search("123456", krx=LISTING, us=US)[-1]["market"] == "KRX"
    assert symbols.search("   ", krx=LISTING, us=US) == []
