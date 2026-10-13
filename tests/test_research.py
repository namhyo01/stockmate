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


def test_add_to_ideas_uses_saved_card_or_extracts(tmp_path, monkeypatch):
    import contextlib
    import json

    from sqlalchemy.orm import Session

    from stockmate import analysis, ideas
    from stockmate.db import make_engine

    session = Session(make_engine("sqlite://"))
    monkeypatch.setattr(research, "session_scope", lambda: contextlib.nullcontext(session))
    monkeypatch.setattr(research, "DIR", tmp_path)
    card = {"name": "현대차", "symbol": "005380", "account": "토스", "horizon": "swing", "summary": "기다릴 자리",
            "stop": 295000, "target": 347500, "entry_low": 300000, "entry_high": 305000}  # fmt: skip
    (tmp_path / "KRX_005380.json").write_text(json.dumps({"name": "현대차", "markdown": "글", "idea": card}), encoding="utf-8")
    idea = research.add_to_ideas(Market.KRX, "005380")
    assert (idea.source, idea.symbol, idea.horizon, int(idea.stop)) == ("search", "005380", "swing", 295000)

    # 카드 없이 저장된 예전 분석은 글에서 뽑고, 뽑은 카드를 저장해 둔다
    (tmp_path / "US_NVDA.json").write_text(json.dumps({"name": "엔비디아", "markdown": "분석 글"}), encoding="utf-8")
    monkeypatch.setattr(analysis, "extract_idea", lambda md, name, symbol: {"name": "x", "summary": "요약", "horizon": "long"})
    idea = research.add_to_ideas(Market.US, "NVDA")
    assert (idea.name, idea.symbol, idea.horizon) == ("엔비디아", "NVDA", "long")
    assert json.loads((tmp_path / "US_NVDA.json").read_text(encoding="utf-8"))["idea"]["summary"] == "요약"
    assert len(ideas.active_ideas(session)) == 2


def test_plan_draft_extracts_once_and_add_to_plans(tmp_path, monkeypatch):
    import contextlib
    import json

    from sqlalchemy.orm import Session

    from stockmate import analysis, plans
    from stockmate.db import make_engine

    session = Session(make_engine("sqlite://"))
    monkeypatch.setattr(research, "session_scope", lambda: contextlib.nullcontext(session))
    monkeypatch.setattr(research, "DIR", tmp_path)
    (tmp_path / "KRX_005380.json").write_text(json.dumps({"name": "현대차", "markdown": "글"}), encoding="utf-8")
    calls = []

    def fake_extract(md, name, symbol):
        calls.append(name)
        return [{"side": "buy", "name": "x", "account": "토스", "quantity": 1, "price_high": 305000}, {"side": "hold"}]

    monkeypatch.setattr(analysis, "extract_plan", fake_extract)
    draft = research.plan_draft(Market.KRX, "005380")
    assert [i["name"] for i in draft["items"]] == ["현대차"] and draft["open"] == []
    research.plan_draft(Market.KRX, "005380")
    assert calls == ["현대차"]  # 한 번 뽑은 초안은 저장해 두고 다시 묻지 않는다

    changes = research.add_to_plans(Market.KRX, "005380", "현대차",
                                    [{"side": "buy", "account": "토스", "quantity": "1", "price_high": "305000", "due_from": ""}])
    item = plans.open_items(session)[0]
    assert (item.source, item.symbol, int(item.price_high), item.due_from) == ("search", "005380", 305000, None)
    assert changes
