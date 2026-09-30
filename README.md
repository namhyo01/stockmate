# 📈 stockmate

여러 증권사(한국투자, 토스, 삼성 등)에 흩어진 주식 계좌를 한곳에 모아 관리하고,
AI로 분석해서 매수·매도 타이밍을 제안하는 **개인용** 투자 비서.

> 분석 결과는 참고용이며, 매매 판단과 책임은 본인에게 있습니다. 자동 주문 기능은 없습니다.

## 시작하기

```bash
# uv 설치 (없다면): curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env.example .env   # 필요한 키를 채워 넣기 (1단계는 키 없이도 동작)
uv run stock --help
```

## 사용법

```bash
# 1) 계좌 등록 (증권사: kis, toss, samsung, other)
uv run stock account add 한투 --broker kis
uv run stock account add 토스 --broker toss
uv run stock account add 삼성연금 --broker samsung --kind pension --monthly 400000  # 종류: general/pension/irp/isa
uv run stock account list

# 2-a) 현재 잔고를 그대로 입력 (종목코드로 국내/미국 자동 판별, 종목명 자동 조회)
uv run stock holding set 토스 AAPL 3.5 210
uv run stock holding set 토스 005930 10 71000

# 2-b) 또는 CSV로 한꺼번에 가져오기
uv run stock import template > my_holdings.csv   # 템플릿 만들기
uv run stock import holdings 삼성 my_holdings.csv            # 추가/덮어쓰기
uv run stock import holdings 삼성 my_holdings.csv --replace  # CSV에 없는 종목은 삭제 (계좌 전체 동기화)

# 2-c) 또는 매매할 때마다 기록 (이동평균 단가, 실현손익 자동 계산)
uv run stock trade buy 한투 005930 10 70000 --date 2026-09-01 --fee 150 --note "실적 기대"
uv run stock trade sell 한투 005930 3 75000 --fee 1000
uv run stock trade list

# 3) 종목별 전략 태그 (long=장기, swing=스윙, short=단기) → AI 분석 기준이 달라짐
uv run stock tag 005930 long

# 4) 전체 현황 (원화 환산, 증권사별·시장별 합계)
uv run stock summary
```

## 구조

```
src/stockmate/
  config.py       환경 설정 (.env)
  models.py       DB 모델: Account, Instrument, Holding, Trade
  db.py           SQLite 연결 (data/stockmate.db)
  portfolio.py    핵심 로직: 매매 기록, 평균단가, 평가, 합계
  market_data.py  시세·환율·종목명 조회 (FinanceDataReader, yfinance)
  importers.py    CSV 가져오기
  cli.py          명령줄 인터페이스
tests/            uv run pytest
```

- 금액은 모두 `Decimal`로 계산하고 SQLite에는 문자열로 저장해서 부동소수점 오차가 없습니다.
- 평균단가는 증권사 앱 표기와 같게 수수료를 뺀 값이고, 수수료는 매도 시 실현손익에서 차감합니다.

## 문서

- `CLAUDE.md`: 구조, 설계 결정, 주의사항 (Claude Code가 먼저 읽는 파일)
- `docs/HANDOFF.md`: 첫 세션 대화 요약, 결정 배경, 남은 일
- `docs/DEPLOY_AWS.md`: AWS(t3a.micro) 배포 절차

## 로드맵

- [x] **1단계** 통합 포트폴리오: 계좌/잔고/매매 기록, CSV 가져오기, 현재가·환율 평가
- [x] **2단계** 한국투자증권 Open API 연동: `stock sync kis` (잔고·예수금 동기화, 모든 보유 종목의 현재가)
- [x] **3단계** 웹 대시보드: `stock web` → http://localhost:8000 (주식 지도, 계좌, 종목, 1분 자동 갱신)
- [x] **4단계** AI 분석 (Claude Code 구독, API 키 불필요): 분석 리포트, 질문하기
- [x] **4-1단계** 기억: 투자자 프로필(`data/memory/profile.md`), 추천 기록과 이후 가격 변화, 최근 대화
- [ ] **4-2단계** 매매 자동 감지와 추천 성적표
- [ ] **5단계** 텔레그램 알림과 스케줄링 (장 전 브리핑, 목표가, 급등락, 공시)
- [ ] **6단계** 백테스팅과 스크리너
