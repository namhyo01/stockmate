from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from stockmate import memory
from stockmate.db import make_engine


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


@pytest.fixture(autouse=True)
def profile_path(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "profile.md"
    monkeypatch.setattr(memory, "PROFILE_PATH", path)
    return path


REPORT = """## 한 줄 요약
좋아요.

> 참고용 분석이며 투자 판단과 책임은 본인에게 있습니다.

```json
{"recommendations": [
  {"symbol": "000660", "account": "토스", "opinion": "비중축소", "reason": "쏠림", "watch_low": 1700000, "watch_high": null},
  {"opinion": "보유"}
], "lessons": ["소형 테마주 손절 기준이 없었다"]}
```
"""


def test_parse_report_strips_json_block():
    parsed = memory.parse_report(REPORT)
    assert parsed.markdown.endswith("본인에게 있습니다.")
    assert "```json" not in parsed.markdown
    assert [r["symbol"] for r in parsed.recommendations] == ["000660"]  # symbol 없는 행은 버린다
    assert parsed.lessons == ["소형 테마주 손절 기준이 없었다"]


def test_parse_report_without_or_with_broken_block():
    assert memory.parse_report("## 요약\n본문").markdown == "## 요약\n본문"
    broken = memory.parse_report('본문\n```json\n{"recommendations": [\n```')
    assert broken.markdown == "본문"
    assert broken.recommendations == []


def test_split_memory_blocks():
    text = "답변입니다.\n```profile\n- 레버리지 싫어함\n```\n```plan\n- 하이닉스 1주 190만원 이상이면 매도\n```"
    answer, notes = memory.split_memory_blocks(text)
    assert answer == "답변입니다."
    assert notes == {"profile": ["- 레버리지 싫어함"], "plan": ["- 하이닉스 1주 190만원 이상이면 매도"]}
    assert memory.split_memory_blocks("그냥 답변") == ("그냥 답변", {"profile": [], "plan": []})


def test_conversations_are_saved_to_monthly_files(profile_path):
    memory.save_conversation("9월 질문", "답\n\n두 문단 답", at=datetime(2026, 9, 30, 23, 59))
    for i in range(6):
        memory.save_conversation(f"10월 질문 {i}", f"답 {i}", at=datetime(2026, 10, 1, 9, i))

    files = sorted(p.name for p in (profile_path.parent / "conversations").glob("*.md"))
    assert files == ["2026-09.md", "2026-10.md"]
    recent = memory.recent_conversations(limit=7)
    assert [c.question for c in recent][:2] == ["9월 질문", "10월 질문 0"]
    assert recent[0].answer == "답\n\n두 문단 답"
    assert [c.question for c in memory.recent_conversations(limit=2)] == ["10월 질문 4", "10월 질문 5"]


def test_add_profile_notes_goes_into_its_section(profile_path):
    added = memory.add_profile_notes("conversation", ["- 레버리지 싫어함", "  "])
    memory.add_profile_notes("lesson", ["손절 기준 필요"])
    memory.add_profile_notes("plan", ["하이닉스 1주 매도"])
    memory.add_profile_notes("conversation", ["배당 선호"])

    assert added == ["레버리지 싫어함"]
    text = profile_path.read_text(encoding="utf-8")
    plans = text.split("## 진행 중인 결정과 계획")[1].split("## 대화에서 알게 된 것")[0]
    assert "하이닉스 1주 매도" in plans and "레버리지" not in plans
    conversation = text.split("## 대화에서 알게 된 것")[1].split("## 지난 추천에서 배운 점")[0]
    lessons = text.split("## 지난 추천에서 배운 점")[1]
    assert "레버리지 싫어함" in conversation and "배당 선호" in conversation
    assert conversation.index("레버리지") < conversation.index("배당")
    assert "손절 기준 필요" in lessons and "레버리지" not in lessons
    assert text.startswith("# 투자자 프로필")


def test_recommendations_track_price_change(session):
    then = {"positions": [{"symbol": "000660", "name": "SK하이닉스", "account": "토스", "price": 1_000_000.0, "currency": "KRW"}]}
    memory.save_recommendations(session, datetime(2026, 9, 1), [{"symbol": "000660", "opinion": "보유", "watch_low": "900,000"}], then)
    memory.save_recommendations(session, datetime(2026, 9, 8), [{"symbol": "000660", "opinion": "비중축소"}], then)
    memory.save_recommendations(session, datetime(2026, 9, 15), [{"symbol": "000660", "opinion": "추가매수"}], then)

    now = {"positions": [{"symbol": "000660", "price": 1_100_000.0}]}
    rows = memory.recommendation_rows(session, now)

    assert [r["opinion"] for r in rows] == ["추가매수", "비중축소"]  # 최근 분석 2회분만
    assert rows[0]["name"] == "SK하이닉스"
    assert rows[0]["change_pct"] == pytest.approx(10.0)


def test_context_includes_profile_recommendations_and_conversations(session):
    memory.save_conversation("하이닉스 팔까?", "일부 익절을 고려해 보세요.")
    context = memory.build_context(session, {"positions": []})
    assert "# 투자자 프로필" in context
    assert "하이닉스 팔까?" in context
    assert "지난 추천" in context


def test_parse_report_reads_json_block_followed_by_sources():
    text = '## 요약\n본문\n```json\n{"recommendations": [{"symbol": "AAPL"}], "lessons": ["교훈"]}\n```\n\nSources: [기사](https://example.com)'
    parsed = memory.parse_report(text)
    assert [r["symbol"] for r in parsed.recommendations] == ["AAPL"] and parsed.lessons == ["교훈"]
    assert parsed.markdown == "## 요약\n본문\n\nSources: [기사](https://example.com)"
