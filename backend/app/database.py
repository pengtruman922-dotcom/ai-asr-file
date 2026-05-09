from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


settings = get_settings()
db_url = settings.database_url
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
connect_args = {}
if db_url.startswith("sqlite"):
    sqlite_path = db_url.replace("sqlite:///", "")
    if sqlite_path.startswith("./"):
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False}

engine = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_compatibility_schema()


def ensure_compatibility_schema() -> None:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "recordings" in table_names:
        existing = {column["name"] for column in inspector.get_columns("recordings")}
        required = {
            "storage_config_id": "VARCHAR(64) DEFAULT 'default'",
            "storage_provider": "VARCHAR(64) DEFAULT ''",
            "storage_bucket_name": "VARCHAR(255) DEFAULT ''",
            "storage_endpoint": "VARCHAR(1024) DEFAULT ''",
            "storage_region": "VARCHAR(128) DEFAULT ''",
            "storage_path_prefix": "VARCHAR(512) DEFAULT ''",
        }
        missing = [(name, ddl) for name, ddl in required.items() if name not in existing]
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing:
                    connection.execute(text(f"ALTER TABLE recordings ADD COLUMN {name} {ddl}"))
    if "qa_messages" in table_names:
        existing_qa = {column["name"] for column in inspector.get_columns("qa_messages")}
        qa_required = {
            "reasoning_content": "TEXT DEFAULT ''",
        }
        qa_missing = [(name, ddl) for name, ddl in qa_required.items() if name not in existing_qa]
        if qa_missing:
            with engine.begin() as connection:
                for name, ddl in qa_missing:
                    connection.execute(text(f"ALTER TABLE qa_messages ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
