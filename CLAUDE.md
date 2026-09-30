# stockmate: 개인 투자 비서

여러 증권사에 흩어진 주식 계좌를 한곳에 모으고, Claude가 분석해서 매매 판단을 돕는 **사용자 한 명 전용** 도구.
사용자와는 **한국어**로 대화한다. 지금까지의 대화와 결정 배경은 `docs/HANDOFF.md`에 정리돼 있으니 먼저 읽는다.

## 명령어

```bash
uv sync                      # 의존성 설치
uv run pytest                # 테스트 (변경 후 항상 실행)
uv run stock --help          # CLI
uv run stock summary         # 통합 현황 (터미널)
uv run stock sync kis        # 한투 잔고·예수금 동기화
uv run stock web             # 웹 대시보드 http://localhost:8000
```

## 구조

```
src/stockmate/
  models.py        Account(계좌), Instrument(종목), Holding(잔고), Trade(매매), Recommendation(추천 기록)
  portfolio.py     매매 기록, 이동평균 단가, 평가, 합계
  market_data.py   PriceProvider 프로토콜, 공개 시세(FinanceDataReader·yfinance), CachedPrices
  brokers/kis.py   한국투자증권 Open API (조회 전용: 잔고, 예수금, 국내·해외 현재가)
  sync.py          KIS 동기화, 시세 제공자 선택 (KIS 우선, 실패 시 공개 데이터)
  importers.py     잔고 CSV 가져오기 (symbol,name,quantity,avg_cost,market)
  report.py        대시보드·분석 공용 스냅샷 dict, ETF 이름으로 실제 투자 지역 추정
  analysis.py      claude -p 실행 (분석 리포트, 질문 답변)
  memory.py        투자자 프로필, 추천 기록, 대화 기록 → 프롬프트용 기억
  web/app.py       FastAPI (JSON API + 정적 페이지, WEB_PASSWORD 있으면 Basic 인증)
  web/static/index.html  대시보드 (바닐라 JS, 빌드 없음)
data/              (git 제외) stockmate.db, memory/, analyses/, imports/, kis_token.json
```

## 핵심 설계 결정 (바꾸기 전에 사용자와 상의)

- **AI는 Anthropic API 키가 아니라 Claude Code 구독으로 돌린다.** `analysis.py`가 `claude -p --tools WebSearch --strict-mcp-config`로 새 세션을 띄운다. anthropic SDK나 ANTHROPIC_API_KEY를 추가하지 않는다.
- **주문 기능은 만들지 않는다.** API 키 유출 대비가 아니라, AI(외부 텍스트를 읽는 claude -p)에게 매매 경로를 주지 않기 위해서다. 사용자가 원하면 "사람이 CLI에서 직접 승인해야 실행 + 금액 한도 + 모의투자 먼저 + cron에서는 불가" 조건으로만 만든다.
- **기억은 파일과 DB로 쌓는다** (모델 재학습이 아님). 분석·질문 때마다 `memory.build_context()`가 프로필, 최근 추천 2회분과 이후 가격 변화, 최근 대화 5개를 프롬프트에 넣는다.
  - `data/memory/profile.md`: 사람이 고칠 수 있는 투자자 프로필. Claude 응답 끝의 ```profile / ```plan 블록, 분석 JSON의 lessons가 자동으로 추가된다.
  - `data/memory/conversations/YYYY-MM.md`: 웹 질문 전체 기록 (화면에는 표시하지 않는다).
  - 분석 리포트 끝의 ```json 블록(recommendations, lessons)을 `memory.parse_report()`가 떼어 DB에 저장한다.
- **금액은 Decimal**, SQLite에는 `DecimalText`(문자열)로 저장한다.
- **색은 한국식**: 상승 빨강, 하락 파랑. 계좌·지역 색은 순위가 아니라 대상에 고정한다.
- 평균단가는 증권사 앱처럼 수수료를 뺀 값이고, 수수료는 매도할 때 실현손익에서 뺀다.

## 계좌별 데이터 입력 방식

| 계좌 (DB 이름) | 종류 | 수량 입력 | 가격 |
|---|---|---|---|
| 토스 | 일반 | 스크린샷 → CSV → `stock import holdings 토스 <csv> --replace` | 한투 시세 API |
| 한투 | ISA 중개형 서민형 | `stock sync kis` (웹에서 5분마다 자동) | 한투 시세 API |
| 삼성연금 | 연금저축, 월 40만원 적립 | 스크린샷이나 사용자 말 (아직 비어 있음) | 한투 시세 API |

**토스 스크린샷 가져오기**: 토스 보유 종목 화면(평가금, 원 보기)에는 평균단가가 없다. `평균단가 = (평가금 − 손익) / 환율 / 수량`으로 역산하고(환율은 화면 상단 값), 실제 시세와 비교해서 티커를 검증한 다음 CSV로 만들어 `--replace`로 동기화한다. 첫 가져오기 예시는 `data/imports/toss_2026-09-30.csv`.

## 배포 (AWS)

- 서버: `ssh stockmate` (Tailscale 경유, AWS 보안 그룹의 인바운드 포트는 0개), 코드는 `~/stockmate`, 서비스는 `stockmate` (systemd)
- 대시보드: https://<서버>.<tailnet>.ts.net (`tailscale serve`, Tailscale에 로그인한 기기에서만 열린다)
- PC에서 고쳤다면: `git push server master` (서버 저장소가 `receive.denyCurrentBranch=updateInstead`라 작업 트리까지 바로 갱신된다) → `ssh stockmate 'sudo systemctl restart stockmate'`
- 서버에서 직접 고쳤다면: 서버에서 커밋하고, PC에서는 `git pull server master`
- 로그: `journalctl -u stockmate -f`

## 주의사항

- KIS: 초당 20건 제한(코드에서 0.1초 간격 + EGW00201 재시도). 토큰 발급은 1분에 1번이라 `data/kis_token.json`에 캐시한다. **키를 재발급하면 이 파일을 지워야 한다.** 미국 종목의 거래소 코드는 `data/kis_exchanges.json`에 캐시한다.
- 서버 시간대는 **Asia/Seoul**이어야 한다 (`datetime.now()`를 곳곳에서 쓴다). SQLite `func.now()` 기본값은 UTC라는 점도 주의.
- `CachedPrices`는 여러 요청 스레드가 공유한다. 상태를 바꿀 때는 반드시 잠금 안에서 한다 (예전에 가격이 전부 빈칸이 되는 경쟁 조건 버그가 있었다).
- `.env`, `data/`, `*.csv`는 절대 커밋하지 않는다.

## 코드 스타일

- 주석, 독스트링, 사용자에게 보이는 문구는 한국어로 쓴다. 화면 문구는 "~요" 체로, 버튼은 동작 그대로 쓴다.
- 새 로직에는 테스트를 붙인다 (`tests/`, 외부 호출 없이 가짜 PriceProvider 사용).
