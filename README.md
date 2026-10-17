# 📈 stockmate

여러 증권사(한국투자증권, 토스증권, 삼성증권 등)에 흩어진 주식 계좌를 한 화면에 모으고,
**Claude Code 구독**으로 AI가 차트·뉴스·포트폴리오를 함께 보고 매매 판단을 돕는 개인용 투자 비서입니다.

> 분석 결과는 참고용이며, 매매 판단과 책임은 본인에게 있습니다. **주문 기능은 일부러 만들지 않았습니다.**

## 무엇을 하나요

**포트폴리오**
- 한국투자증권·토스증권 Open API로 잔고와 예수금을 1분마다 동기화 (조회 전용)
- API가 없는 계좌(예: 삼성증권 연금저축)는 앱 **스크린샷을 올리면 AI가 읽어서** 잔고를 맞춤 (미리보기에서 확인 후 반영)
- 원화 환산 손익(미국 주식은 매입 평균 환율까지 반영), 계좌·지역별 비중, 수익률 색으로 칠한 주식 지도
- 기간 성과(하루·1주·1개월·3개월·1년)와 지수 비교, 많이 오르고 내린 종목
- 화면 위 시장 지표 띠(코스피·코스닥·S&P 500·나스닥·환율·금리·비트코인), 누르면 차트

**차트** (라이브러리 없이 SVG)
- 일봉 캔들, 이동평균, 일목균형표(구름·후행스팬·앞 구름), 볼린저밴드, 거래량, 평단선(계좌별)
- '오늘' 1분봉, 기간의 최고가·최저가
- **AI 가격선**: AI가 낸 매수 구간·철회 기준·목표가와 매매 계획 가격을 차트 위에

**AI** (Anthropic API 키 없이 `claude -p`로 Claude Code 구독을 사용)
- 포트폴리오 분석 리포트: 웹 검색 + 차트 점검 + 계좌 규칙(ISA·연금저축 등)을 반영한 이번 달 실행 계획
- 질문하기: 질문에 나온 종목(한글 이름·티커·코드)을 찾아 차트 지표를 붙여서 답함
- 종목 검색 탭: 국내·미국 약 11,000종목 검색 → 차트 → AI 종목 분석 → 추천·계획에 담기
- 매주 토요일 종목 발굴: 시장 약 1,000종목을 훑고, **차트 패턴의 과거 성과를 백테스트한 숫자**와 함께 AI가 3~5개 추천
- 추천 성적표: 추천을 지수(KODEX 200·코스닥150·SPY)와 비교해서 매기고 다음 추천에 반영
- 기억: 투자자 프로필, 지난 추천과 이후 가격 변화, 최근 대화를 파일과 DB에 쌓아 프롬프트에 넣음

## 설계 원칙

- **지표는 코드가 계산하고 AI는 해석만 한다.** AI가 숫자를 지어내지 않게, 이동평균·일목·RSI·지지/저항 등은 파이썬으로 계산해서 넘깁니다.
- **AI에게 매매 경로를 주지 않는다.** 웹 검색 결과를 읽는 AI는 프롬프트 인젝션에 노출될 수 있어서, AI가 할 수 있는 건 글을 쓰는 것뿐입니다. 매매 계획도 사람이 확인해야 담깁니다.
- **확률을 숨기지 않는다.** 백테스트에서 단순 차트 패턴은 대부분 지수를 이기지 못했고(상대강도 계열만 꾸준), 이긴 패턴도 중간값은 마이너스였습니다. 화면과 프롬프트에 그대로 보여주고, 추천에는 철회 기준을 꼭 붙입니다. 생존 편향도 밝힙니다.
- 금액은 `Decimal`, 색은 한국식(상승 빨강, 하락 파랑).

## 시작하기

준비물: Docker, [Claude Code](https://docs.anthropic.com/claude-code) 구독, (선택) 한국투자증권·토스증권 Open API 키

```bash
cp .env.example .env
# .env 채우기: KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO, TOSS_CLIENT_ID / TOSS_CLIENT_SECRET (없으면 비워 둠)
# AI용 구독 토큰: claude setup-token 으로 만든 값을 CLAUDE_CODE_OAUTH_TOKEN= 에
docker compose up -d --build
open http://localhost:8000
```

Docker 없이 실행하려면:

```bash
# uv 설치: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run stock web          # http://localhost:8000
uv run pytest             # 테스트
```

계좌를 직접 넣을 수도 있어요.

```bash
uv run stock account add 한투 --broker kis --kind isa
uv run stock account add 토스 --broker toss
uv run stock sync kis && uv run stock sync toss      # API로 동기화
uv run stock holding set 토스 AAPL 3.5 210            # 또는 직접 입력
uv run stock import holdings 토스 my.csv --replace    # 또는 CSV
uv run stock discover --no-ai                         # 종목 발굴 (패턴 백테스트만)
```

> 대시보드는 `127.0.0.1`에만 열립니다. 밖에서 볼 때는 Tailscale 같은 사설망을 쓰고, 공개 인터넷에 열어야 하면 `.env`의 `WEB_PASSWORD`를 꼭 설정하세요.

## 맞춤 설정이 필요한 부분

한 사람의 실제 계좌(토스 일반 / 한투 ISA / 삼성증권 연금저축)에 맞춰 만든 도구라, 다른 구성이라면 아래를 고쳐서 쓰세요.

- `src/stockmate/analysis.py`의 분석 프롬프트에 있는 **계좌 규칙**(어떤 계좌에 무엇을 살 수 있는지, 월 적립액)
- `src/stockmate/plans.py`의 계좌 이름 정규화(`normalize_account`)
- 시세는 한국투자증권 시세 API를 우선 쓰고, 없으면 무료 공개 데이터(FinanceDataReader·yfinance, 지연될 수 있음)를 씁니다.

## 구조

```
src/stockmate/
  brokers/          한국투자증권·토스증권 Open API (조회 전용)
  portfolio.py      매매 기록, 평균단가, 평가, 합계
  technical.py      차트 지표 계산 → AI용 요약
  patterns.py       발굴용 차트 패턴과 백테스트
  discovery.py      매주 종목 발굴, 추천 성적표
  analysis.py       claude -p 실행 (분석, 질문)
  research.py       검색 탭 종목 분석, AI 가격선
  memory.py ideas.py plans.py   기억, 추천 종목, 매매 계획
  web/              FastAPI + 바닐라 JS 대시보드 (빌드 없음)
tests/              uv run pytest (외부 호출 없이)
```

자세한 설계 결정과 주의사항은 [`CLAUDE.md`](CLAUDE.md)에 있습니다 (Claude Code가 먼저 읽는 파일).

## 라이선스

[MIT](LICENSE)
