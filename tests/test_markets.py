import datetime as dt

from stockmate import markets


def test_parse_naver_index_and_market_index():
    index = {"closePrice": "6,015.11", "compareToPreviousClosePrice": "-369.13", "fluctuationsRatio": "-5.78",
             "marketStatus": "OPEN", "localTradedAt": "2026-10-13T10:37:00+09:00"}  # fmt: skip
    q = markets.parse_naver("kospi", "코스피", "", index)
    assert (q.value, q.change, q.change_pct, q.status) == (6015.11, -369.13, -5.78, "OPEN")
    fx = {"result": {"closePrice": "1,338.40", "fluctuations": "2.40", "fluctuationsRatio": "0.18", "marketStatus": "OPEN"}}
    q = markets.parse_naver("usdkrw", "원/달러", "원", fx)
    assert (q.value, q.change, q.unit) == (1338.4, 2.4, "원")
    assert markets.parse_naver("x", "x", "", {"closePrice": None}) is None


def test_board_caches_and_keeps_last_value_on_failure():
    calls = {"n": 0}

    def fetch(item):
        calls["n"] += 1
        key = item[0]
        if calls["n"] > len(markets.INDICATORS) and key == "kospi":
            return None  # 두 번째에는 코스피만 실패
        return markets.Quote(key, item[1], 1.0, 0.0, 0.0, "OPEN", None, item[4])

    board = markets.MarketBoard(ttl=dt.timedelta(seconds=60), fetch=fetch)
    first = board.get()
    assert len(first) == len(markets.INDICATORS) and first[0]["key"] == "kospi"
    board.get()
    assert calls["n"] == len(markets.INDICATORS)  # 캐시에서 꺼낸다
    board._fetched_at = None
    assert [q["key"] for q in board.get()] == [q["key"] for q in first]  # 실패한 코스피도 지난 값으로 남는다
