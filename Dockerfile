# stockmate 웹 대시보드 컨테이너 (맥 Docker Desktop). 실행: docker compose up -d --build
# 코드와 data/는 compose가 /app에 그대로 연결한다. 파이썬 패키지는 /opt/venv에 따로 둔다.
FROM python:3.12-slim-bookworm

ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1 \
    PATH=/home/app/.local/bin:/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN useradd -m app && mkdir -p /opt/venv /app && chown app:app /opt/venv /app
USER app
# 분석(claude -p)용 Claude Code. 인증은 .env의 CLAUDE_CODE_OAUTH_TOKEN(구독 토큰)으로 한다.
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /app
COPY --chown=app:app pyproject.toml uv.lock README.md ./
COPY --chown=app:app src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uvicorn", "stockmate.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
