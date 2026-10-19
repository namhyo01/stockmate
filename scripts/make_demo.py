"""데모 데이터 만들기: 증권사 키 없이 대시보드를 둘러볼 수 있게 예시 계좌·잔고·계획·추천·AI 의견을 넣는다.

실제 data/와 섞이지 않게 반드시 따로 폴더를 정해서 쓴다 (DB_PATH가 없거나 기본 data/를 가리키면 멈춘다).

    DB_PATH=demo/stockmate.db uv run python scripts/make_demo.py
    DB_PATH=demo/stockmate.db KIS_APP_KEY= TOSS_CLIENT_ID= uv run stock web --port 8010

보유 종목과 금액은 모두 지어낸 예시이고, 시세만 공개 데이터(FinanceDataReader·yfinance)를 쓴다.
AI 의견·추천 카드도 화면을 보여주기 위한 예시 글이다 (실제 AI 분석이 아니다).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
db_env = os.environ.get("DB_PATH")
if not db_env or Path(db_env).resolve() == (ROOT / "data" / "stockmate.db").resolve():
    sys.exit("DB_PATH를 데모용 경로로 정해 주세요. 예: DB_PATH=demo/stockmate.db uv run python scripts/make_demo.py")
if Path(db_env).exists():
    sys.exit(f"{db_env}가 이미 있어요. 지우고 다시 실행하세요 (덮어쓰지 않아요).")

from stockmate import ideas, memory, plans, portfolio  # noqa: E402  (DB_PATH를 확인한 뒤에 불러온다)
from stockmate.db import session_scope  # noqa: E402
from stockmate.market_data import PublicMarketData  # noqa: E402
from stockmate.models import AccountKind, Broker, Market, Strategy  # noqa: E402
from stockmate.research import DIR as RESEARCH_DIR  # noqa: E402

ACCOUNTS = [
    ("토스", Broker.TOSS, AccountKind.GENERAL, None, D(1_250_000), D(820)),
    ("한투", Broker.KIS, AccountKind.ISA, None, D(310_000), None),
    ("삼성연금", Broker.SAMSUNG, AccountKind.PENSION, D(400_000), D(160_000), None),
]
# (계좌, 종목, 이름, 수량, 평단, 매입 환율, 전략)
HOLDINGS = [
    ("토스", "005930", "삼성전자", 12, 182_000, None, Strategy.LONG),
    ("토스", "000660", "SK하이닉스", 2, 980_000, None, Strategy.SWING),
    ("토스", "035420", "NAVER", 5, 205_000, None, Strategy.LONG),
    ("토스", "AAPL", "애플", 4, 228.5, 1365, Strategy.LONG),
    ("토스", "NVDA", "엔비디아", 6, 152.3, 1380, Strategy.SWING),
    ("토스", "MSFT", "마이크로소프트", 2, 455.0, 1370, Strategy.LONG),
    ("토스", "AMZN", "아마존", 3, 215.0, 1385, Strategy.LONG),
    ("한투", "360750", "TIGER 미국S&P500", 40, 23_800, None, Strategy.LONG),
    ("한투", "069500", "KODEX 200", 15, 82_000, None, Strategy.LONG),
    ("한투", "458730", "TIGER 미국배당다우존스", 60, 13_900, None, Strategy.LONG),
    ("삼성연금", "360750", "TIGER 미국S&P500", 25, 25_400, None, Strategy.LONG),
    ("삼성연금", "133690", "TIGER 미국나스닥100", 2, 168_000, None, Strategy.LONG),
]

PROFILE = """# 투자자 프로필 (데모)

- 국내·미국을 섞어서, 장기 보유가 중심이고 일부는 몇 주~몇 달 스윙
- 레버리지 ETF는 쓰지 않는다
- 연금은 매달 S&P500 ETF로 적립, ISA는 지수·배당 ETF 위주
- 한 종목이 전체의 20%를 넘지 않게 한다
"""


def main() -> None:
    today = dt.date.today()
    with session_scope() as session:
        accounts = {}
        for name, broker, kind, monthly, cash, cash_usd in ACCOUNTS:
            a = portfolio.create_account(session, name, broker, kind, monthly)
            a.cash, a.cash_usd, a.synced_at = cash, cash_usd, dt.datetime.now()
            accounts[name] = a
        for acct, symbol, name, qty, avg, fx, strategy in HOLDINGS:
            inst = portfolio.get_or_create_instrument(session, symbol, name=name)
            inst.strategy = strategy
            portfolio.set_holding(session, accounts[acct], inst, D(qty), D(str(avg)), D(fx) if fx else None)

    # 지금 시세를 기준으로 계획·추천·AI 가격선을 만든다 (언제 실행해도 화면에 그럴듯하게 보이게)
    keys = [(Market.US, "NVDA"), (Market.US, "COST"), (Market.KRX, "000660"), (Market.KRX, "360750")]
    prices = {k: float(v) for k, v in PublicMarketData().last_prices(keys).items()}
    nvda, cost = prices.get((Market.US, "NVDA"), 180.0), prices.get((Market.US, "COST"), 900.0)
    hynix, sp = prices.get((Market.KRX, "000660"), 1_500_000.0), prices.get((Market.KRX, "360750"), 25_000.0)
    r = lambda v, step: round(v / step) * step  # noqa: E731
    monday = today - dt.timedelta(days=today.weekday())

    with session_scope() as session:
        plans.apply_actions(session, {"add": [
            {"side": "buy", "name": "엔비디아", "symbol": "NVDA", "account": "토스", "quantity": 1,
             "price_high": round(nvda * 0.94, 1), "timing": "실적 발표 뒤 20일선 근처로 눌리면", "reason": "주도주 눌림목, 나눠서 추가"},
            {"side": "sell", "name": "SK하이닉스", "symbol": "000660", "account": "토스", "quantity": 1,
             "price_low": r(hynix * 1.08, 1000), "timing": "52주 고가 부근 저항에서", "reason": "반도체 비중 줄이기"},
            {"side": "buy", "name": "TIGER 미국S&P500", "symbol": "360750", "account": "한투", "quantity": 10,
             "timing": "다음 주 적립일", "due_from": (monday + dt.timedelta(days=7)).isoformat(),
             "due_to": (monday + dt.timedelta(days=11)).isoformat(), "reason": "코어 지수 적립"},
            {"side": "buy", "name": "TIGER 미국S&P500", "symbol": "360750", "account": "삼성연금", "quantity": 15,
             "price_high": r(sp * 1.02, 5), "timing": "이번 달 연금 적립분", "due_from": today.isoformat(),
             "due_to": (monday + dt.timedelta(days=4)).isoformat(), "reason": "월 40만원 적립"},
        ]}, "analysis")  # fmt: skip
        ideas.apply_ideas(session, [
            {"name": "코스트코", "symbol": "COST", "account": "토스", "horizon": "long", "patterns": ["rs_leader"],
             "summary": "(데모) 경기에 덜 흔들리는 회원제 유통. 빅테크 쏠림을 덜어줄 방어 몫",
             "thesis": ["회원비가 이익의 큰 부분이라 경기 둔화에도 실적이 꾸준해요", "지금 포트폴리오에 필수소비재가 없어요"],
             "risks": ["PER이 높아 실적이 기대에 못 미치면 크게 흔들릴 수 있어요"], "overlap": "S&P500 ETF에 조금 들어 있어요",
             "entry_low": round(cost * 0.95, 1), "entry_high": round(cost * 0.99, 1),
             "stop": round(cost * 0.88, 1), "target": round(cost * 1.15, 1)},
            {"name": "TIGER 미국배당다우존스", "symbol": "458730", "account": "삼성연금", "horizon": "long",
             "summary": "(데모) 연금에 담기 좋은 배당 ETF. S&P500만 적립하는 것보다 변동이 작아요",
             "thesis": ["분배금 재투자로 장기 복리", "연금 계좌라 분배금 과세가 늦춰져요"], "risks": ["금리가 오르면 배당주가 약할 수 있어요"],
             "overlap": "한투 ISA에도 같은 ETF가 있어요"},
        ], "weekly")  # fmt: skip

    # 검색 탭 AI 분석 예시 (차트의 'AI 의견과 가격선'에 보인다)
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    (RESEARCH_DIR / "US_NVDA.json").write_text(json.dumps({
        "created_at": dt.datetime.now().replace(microsecond=0).isoformat(), "market": "US", "symbol": "NVDA", "name": "엔비디아",
        "markdown": "## 한 줄 결론\n**기다릴 자리**예요. (데모 예시) 추세는 위쪽이지만 단기로는 조정 중이라, 20일선 근처까지 눌릴 때 나눠서 사요.\n\n"
                    "## 차트\n- 이동평균 정배열, 구름 위\n- MACD는 꺾이는 중\n\n> 참고용 분석이며 투자 판단과 책임은 본인에게 있습니다.",
        "idea": {"entry_low": round(nvda * 0.92, 1), "entry_high": round(nvda * 0.96, 1), "stop": round(nvda * 0.86, 1),
                 "target": round(nvda * 1.12, 1), "horizon": "swing"},
        "plan": [{"side": "buy", "timing": "실적 발표 뒤 20일선 근처로 눌리면"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")  # fmt: skip

    memory.write_profile(PROFILE)
    memory.save_conversation("(데모) 이번 달 연금 40만원은 뭘 살까?",
                             "(데모 답변) TIGER 미국S&P500 15주를 이번 주 안에 나눠 사는 걸 추천해요. 계획 탭에 담아 뒀어요.")
    print(f"데모 데이터를 만들었어요: {db_env}")


if __name__ == "__main__":
    main()
