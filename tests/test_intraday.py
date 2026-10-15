from stockmate import intraday

XML = """<chartdata>
<item data="202610141530|null|null|null|200000|100" />
<item data="202610150900|null|null|null|210000|1000" />
<item data="202610150901|null|null|null|212000|1500" />
<item data="202610150902|null|null|null|205000|2600" />
<item data="202610151530|null|null|null|209500|4000" />
</chartdata>"""


def test_parse_naver_keeps_last_day_and_diffs_volume():
    rows = intraday.parse_naver(XML)
    assert [r[0].strftime("%m-%d %H:%M") for r in rows] == ["10-15 09:00", "10-15 09:01", "10-15 09:02", "10-15 15:30"]
    assert [r[2] for r in rows] == [1000, 500, 1100, 1400]  # 누적 거래량의 차이


def test_summarize_high_low_and_change():
    s = intraday.summarize(intraday.parse_naver(XML), 200000)
    assert s["high"] == {"time": "09:01", "price": 212000} and s["low"] == {"time": "09:02", "price": 205000}
    assert s["open"] == 210000 and s["last"] == 209500 and round(s["change_pct"], 2) == 4.75
    assert intraday.summarize([], None) is None
