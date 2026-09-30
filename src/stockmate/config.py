"""환경 설정. 민감한 값(API 키 등)은 프로젝트 루트의 .env 파일에서 읽는다."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = PROJECT_ROOT / "data" / "stockmate.db"

    # 2단계: 한국투자증권 Open API
    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_account_no: str | None = None

    # 4단계: 공시 수집 (AI 분석은 API 키 없이 Claude Code 구독으로 한다)
    dart_api_key: str | None = None

    # 웹 대시보드. 외부에 공개할 때(AWS 등)는 반드시 비밀번호를 설정한다.
    web_password: str | None = None
    # AI 분석에 쓸 Claude Code 실행 파일 (구독 로그인 상태여야 한다)
    claude_bin: str = "claude"

    # 5단계: 알림
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


settings = Settings()
