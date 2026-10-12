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
uv run stock sync toss       # 토스 잔고·주문 가능 현금 동기화
uv run stock web             # 웹 대시보드 http://localhost:8000
```

## 구조

```
src/stockmate/
  models.py        Account(계좌), Instrument(종목), Holding(잔고), Trade(매매), Recommendation(추천 기록), PlanItem(매매 계획), Idea(AI 추천 종목)
  portfolio.py     매매 기록, 이동평균 단가, 평가, 합계
  market_data.py   PriceProvider 프로토콜(+선택: previous_closes 전일 종가 → 오늘 등락), 공개 시세(FinanceDataReader·yfinance), CachedPrices
  brokers/kis.py   한국투자증권 Open API (조회 전용: 잔고, 예수금, 국내·해외 현재가)
  brokers/toss.py  토스증권 Open API (조회 전용: 허용 경로의 GET만 보낸다. 키 자체는 주문 권한이 있다)
  sync.py          KIS·토스 동기화, 시세 제공자 선택 (KIS 우선, 실패 시 공개 데이터)
  importers.py     잔고 CSV 가져오기 (symbol,name,quantity,avg_cost,market)
  report.py        대시보드·분석 공용 스냅샷 dict, ETF 이름으로 실제 투자 지역 추정
  technical.py     일봉(FDR·yfinance, 30분 캐시) → 이동평균·일목균형표·RSI·MACD·볼린저·거래량·캔들 패턴 계산 → 프롬프트용 차트 요약 (AI는 해석만)
  analysis.py      claude -p 실행 (분석 리포트, 질문 답변)
  screenshots.py   스크린샷 → claude -p(도구 없음, JSON 스키마)로 화면 값 읽기 → 평단 역산·티커 검증·잔고 비교 → 반영
  memory.py        투자자 프로필, 추천 기록, 대화 기록 → 프롬프트용 기억
  ideas.py         AI 추천 종목: ```ideas 블록(JSON 배열) → Idea (이유·리스크·겹침·출처, 추천 때 가격 대비 변화)
  plans.py         매매 계획: Claude 응답의 ```actions 블록(add/done/cancel) → PlanItem, 계획 탭용 조건 충족 여부
  web/app.py       FastAPI (JSON API + 정적 페이지, WEB_PASSWORD 있으면 Basic 인증)
  web/static/index.html  대시보드 (바닐라 JS, 빌드 없음). 탭 5개: #portfolio, #plans(매매 계획), #ideas(AI 추천 종목), #ai(대화, 예전 #claude), #analysis(리포트·기억). 화면 문구는 "Claude" 대신 "AI"로 쓴다
data/              (git 제외) stockmate.db, memory/, analyses/, imports/, kis_token.json, toss_token.json
```

## 핵심 설계 결정 (바꾸기 전에 사용자와 상의)

- **AI는 Anthropic API 키가 아니라 Claude Code 구독으로 돌린다.** `analysis.py`가 `claude -p --tools WebSearch --strict-mcp-config`로 새 세션을 띄운다. anthropic SDK나 ANTHROPIC_API_KEY를 추가하지 않는다.
- **주문 기능은 만들지 않는다.** API 키 유출 대비가 아니라, AI(외부 텍스트를 읽는 claude -p)에게 매매 경로를 주지 않기 위해서다. 사용자가 원하면 "사람이 CLI에서 직접 승인해야 실행 + 금액 한도 + 모의투자 먼저 + cron에서는 불가" 조건으로만 만든다.
- **차트 지표는 코드가 계산하고 AI는 해석만 한다.** 분석·질문 프롬프트에 보유 종목, 추천 탭 종목, 질문에 쓴 6자리 코드의 `technical.chart_context()` 요약이 들어간다. 분석 리포트에는 "차트 점검" 섹션이 있다.
- **기억은 파일과 DB로 쌓는다** (모델 재학습이 아님). 분석·질문 때마다 `memory.build_context()`가 프로필, 최근 추천 2회분과 이후 가격 변화, 최근 대화 5개를 프롬프트에 넣는다.
  - `data/memory/profile.md`: 사람이 고칠 수 있는 투자자 프로필. Claude 응답 끝의 ```profile / ```plan 블록, 분석 JSON의 lessons가 자동으로 추가된다.
  - `data/memory/conversations/YYYY-MM.md`: 웹 질문 전체 기록. 대시보드 "지난 질문"에 최신순으로 보인다 (`/api/conversations`).
  - 분석 리포트 끝의 ```json 블록(recommendations, lessons, actions)을 `memory.parse_report()`가 떼어 DB에 저장한다.
  - **매매 계획**(`plans.py`): 질문 답변 끝의 ```actions 블록과 분석 JSON의 actions가 PlanItem으로 쌓인다. 같은 종목·방향·계좌의 열린 계획은 새 것이 대신한다(replaced). 열린 계획은 #번호와 함께 프롬프트에 들어가서 Claude가 done/cancel할 수 있다. 사용자는 계획 탭에서 '했어요'/'취소'로 닫는다. 계획 탭은 계좌별(토스 / 연금=삼성연금 / ISA=한투)로 묶어 보여주므로, 계획의 account는 이 셋 중 하나여야 한다(`plans.normalize_account`가 "ISA", "연금" 같은 표기를 맞춘다). 계좌 안에서는 실행 날짜 범위(`due_from`~`due_to`)로 이번 주 / 다음 주 / 이번 달 / 다음 달 이후 / 날짜 없음(가격·사건 조건)으로 묶는다. 날짜가 빈 예전 계획은 `analysis.fill_plan_dates()`가 시기 글을 읽고 채운다("목록 다시 정리" 때도 실행). "목록 다시 정리" 버튼(`analysis.rebuild_plans`, 매매는 하지 않음)은 최근 대화 12개와 리포트를 다시 읽고 목록을 맞춘다.
- **금액은 Decimal**, SQLite에는 `DecimalText`(문자열)로 저장한다.
- **색은 한국식**: 상승 빨강, 하락 파랑. 계좌·지역 색은 순위가 아니라 대상에 고정한다.
- 평균단가는 증권사 앱처럼 수수료를 뺀 값이고, 수수료는 매도할 때 실현손익에서 뺀다.

## 계좌별 데이터 입력 방식

| 계좌 (DB 이름) | 종류 | 수량 입력 | 가격 |
|---|---|---|---|
| 토스 | 일반 | `stock sync toss` (웹에서 5분마다 자동) | 한투 시세 API |
| 한투 | ISA 중개형 서민형 | `stock sync kis` (웹에서 5분마다 자동) | 한투 시세 API |
| 삼성연금 | 연금저축, 월 40만원 적립 | 웹 계좌 목록의 "스크린샷 올리기" (아직 비어 있음) | 한투 시세 API |

**미국 종목 원화 원금**: `Holding.avg_fx`(매입 평균 환율)로 `수량 × 달러 평단 × avg_fx`를 계산해서 원화 손익에 환율 손익까지 넣는다(토스 앱 "원화 보기"와 같게). 토스 동기화 때 `portfolio.blend_avg_fx()`가 수량 변화에 맞춰 갱신한다(늘면 지금 환율로 가중평균, 같으면 원화 원금 보존, 줄면 비율대로). avg_fx가 없으면 현재 환율로 계산한다. 2026-10-02 첫 토스 API 동기화 때 9/30 스크린샷의 원화 매입금으로 초깃값을 넣었다.

**스크린샷 가져오기** (`screenshots.py`, 웹 `/api/screenshots/read` → `/apply`): 토스 보유 종목 화면(평가금, 원 보기)에는 평균단가가 없어서 `평균단가 = (평가금 − 손익) / 환율 / 수량`으로 역산한다(환율은 화면 값 우선). 화면 값으로 구한 현재가를 실제 시세와 비교해서 8% 넘게 다르면 티커를 의심한다. **MMF·CMA·RP 같은 현금성 상품은 종목이 아니라 예수금**으로 넣는다(사용자 원칙). 화면에 수량이 없으면(삼성 연금 잔고 화면) 평가금액 ÷ 지금 시세로 구하고(국내는 정수에 3% 안쪽일 때만) "추정"으로 표시한다. 미리보기에서 사람이 확인한 행만 반영하고, 화면에 없는 종목은 "지우기"를 체크했을 때만 지운다(화면 총 평가금과 합계가 맞을 때만 기본 체크). 올린 이미지와 읽은 결과는 `data/imports/screenshots/`에 남는다. CLI로 할 때는 CSV를 만들어 `stock import holdings 토스 <csv> --replace` (예시: `data/imports/toss_2026-09-30.csv`).

## 실행 환경 (맥 + Docker)

- 2026-10-09에 AWS에서 사용자의 맥 `<프로젝트 폴더>`로 옮겼다 (AWS는 2026-10-09에 삭제했다). 맥은 상시 켜져 있다.
- **Docker 컨테이너 `stockmate`**로 상시 실행한다 (`compose.yaml`, `restart: unless-stopped`). 코드와 `data/`는 맥 폴더를 그대로 연결하므로, 코드를 고치면 `docker compose restart`, 의존성(pyproject)을 바꾸면 `docker compose up -d --build`.
- **DB를 쓰는 CLI는 컨테이너 안에서**: `docker compose exec stockmate stock summary` (맥에서 직접 같은 SQLite를 열면 Docker 파일 공유를 사이에 둔 잠금 문제로 깨질 수 있다). 테스트(`uv run pytest`)는 맥에서 돌려도 된다 (메모리 DB).
- 컨테이너의 `claude -p`는 `.env`의 `CLAUDE_CODE_OAUTH_TOKEN`(사용자가 `claude setup-token`으로 만든 구독 토큰)으로 인증한다.
- SSH로 들어와서 docker를 쓸 때는 먼저 `source ~/.docker-ssh/env.sh` (SSH 세션은 키체인에 접근하지 못한다).
- 대시보드: https://<서버>.<tailnet>.ts.net (맥의 `tailscale serve`: `/` → 8000, `/pickmap`·`:8443` → pickmap 8001). Tailscale 이름 `stockmate`가 이제 맥이다.
- PC(WSL)의 `server` 리모트는 `stockmate:<프로젝트 폴더>` (`receive.denyCurrentBranch=updateInstead`). PC에서 고쳤다면 `git push server master` 뒤 맥에서 `docker compose restart`.
- 로그: `docker compose logs -f stockmate`

## 주의사항

- 토스: 토스 개발자 콘솔에 등록한 허용 IP에서만 호출된다. 지금은 집 공인 IP(<집 공인 IP>)가 등록돼 있어서 맥에서 호출된다 (예전 AWS Elastic IP <서버 공인 IP>도 있었음). **집 IP가 바뀌면 토스 콘솔에서 다시 등록해야 한다**. 토큰은 키당 1개만 유효해서 새로 받으면 이전 토큰이 즉시 무효가 된다 → `data/toss_token.json`에 캐시하고, 같은 키를 다른 곳(LLM 연동 등)에서 쓰지 않는다.
- KIS: 초당 20건 제한(코드에서 0.1초 간격 + EGW00201 재시도). 토큰 발급은 1분에 1번이라 `data/kis_token.json`에 캐시한다. **키를 재발급하면 이 파일을 지워야 한다.** 미국 종목의 거래소 코드는 `data/kis_exchanges.json`에 캐시한다.
- 실행 시간대는 **Asia/Seoul**이어야 한다 (Dockerfile에서 TZ를 지정했다) (`datetime.now()`를 곳곳에서 쓴다). SQLite `func.now()` 기본값은 UTC라는 점도 주의.
- `CachedPrices`는 여러 요청 스레드가 공유한다. 상태를 바꿀 때는 반드시 잠금 안에서 한다 (예전에 가격이 전부 빈칸이 되는 경쟁 조건 버그가 있었다).
- `.env`, `data/`, `*.csv`는 절대 커밋하지 않는다.

## 코드 스타일

- 주석, 독스트링, 사용자에게 보이는 문구는 한국어로 쓴다. 화면 문구는 "~요" 체로, 버튼은 동작 그대로 쓴다.
- 새 로직에는 테스트를 붙인다 (`tests/`, 외부 호출 없이 가짜 PriceProvider 사용).
