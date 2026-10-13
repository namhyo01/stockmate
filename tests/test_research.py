from stockmate import research
from stockmate.models import Market


def test_position_of_and_patterns_text():
    snapshot = {"positions": [
        {"market": "US", "symbol": "NVDA", "account": "토스", "quantity": 0.05, "avg_cost": 180.0, "pnl_pct": 25.0,
         "value_krw": 15000, "weight": 0.1},
        {"market": "KRX", "symbol": "005930", "account": "토스", "quantity": 3, "avg_cost": 70000, "pnl_pct": 1.0,
         "value_krw": 1, "weight": 1},
    ]}  # fmt: skip
    assert [p["account"] for p in research.position_of(snapshot, Market.US, "NVDA")] == ["토스"]
    assert research.position_of(snapshot, Market.US, "AAPL") == []
    hits = [
        {"label": "주도주 눌림목", "date": "2026-10-12", "description": "설명", "days": 60,
         "stats": {"count": 100, "mean": 5.0, "excess_mean": 1.1, "excess_median": -1.8, "beat_rate": 45, "proven": True}},
        {"label": "구름 돌파", "date": "2026-10-10", "description": "설명", "days": 20, "stats": None},
    ]  # fmt: skip
    text = research._patterns_text(hits)
    assert "지수 대비 평균 +1.1%p" in text and "꾸준히 통함" in text and "과거 성과 기록 없음" in text
    assert research._patterns_text([]) == "(없음)"


def test_saved_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DIR", tmp_path)
    assert research.saved(Market.KRX, "005930") is None
    (tmp_path / "KRX_005930.json").write_text('{"markdown": "분석"}', encoding="utf-8")
    assert research.saved(Market.KRX, "005930") == {"markdown": "분석"}
