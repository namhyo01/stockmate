"""DB 연결과 세션 관리."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from stockmate.config import settings
from stockmate.models import Base

_engine: Engine | None = None

# create_all은 이미 있는 테이블에 컬럼을 추가하지 않으므로, 나중에 생긴 컬럼은 여기서 보충한다.
_ADDED_COLUMNS = {
    "accounts": {"cash": "VARCHAR", "synced_at": "DATETIME", "cash_usd": "VARCHAR"},
    "holdings": {"avg_fx": "VARCHAR"},
    "plan_items": {"due_from": "DATE", "due_to": "DATE", "due_checked": "BOOLEAN DEFAULT 0"},
    "ideas": {"horizon": "VARCHAR(10)", "patterns": "VARCHAR(200)", "target": "VARCHAR", "stop": "VARCHAR"},
}


def _add_missing_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def make_engine(url: str) -> Engine:
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = make_engine(f"sqlite:///{settings.db_path}")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """블록이 정상 종료되면 커밋하고, 예외가 나면 롤백한다."""
    with Session(get_engine(), expire_on_commit=False) as session, session.begin():
        yield session
