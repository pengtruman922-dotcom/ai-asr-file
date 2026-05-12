from contextlib import contextmanager
import hashlib
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
    ensure_default_admin()


def ensure_compatibility_schema() -> None:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "projects" in table_names:
        existing_projects = {column["name"] for column in inspector.get_columns("projects")}
        project_required = {
            "owner_id": "VARCHAR(64)",
            "is_shared": "BOOLEAN DEFAULT false",
        }
        project_missing = [(name, ddl) for name, ddl in project_required.items() if name not in existing_projects]
        if project_missing:
            with engine.begin() as connection:
                for name, ddl in project_missing:
                    connection.execute(text(f"ALTER TABLE projects ADD COLUMN {name} {ddl}"))
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
    if "processing_jobs" in table_names:
        existing_jobs = {column["name"] for column in inspector.get_columns("processing_jobs")}
        job_required = {
            "file_id": "VARCHAR(64)",
            "user_id": "VARCHAR(64)",
        }
        job_missing = [(name, ddl) for name, ddl in job_required.items() if name not in existing_jobs]
        if job_missing:
            with engine.begin() as connection:
                for name, ddl in job_missing:
                    connection.execute(text(f"ALTER TABLE processing_jobs ADD COLUMN {name} {ddl}"))
    if "usage_records" in table_names:
        existing_usage = {column["name"] for column in inspector.get_columns("usage_records")}
        usage_required = {
            "file_id": "VARCHAR(64)",
            "user_id": "VARCHAR(64)",
        }
        usage_missing = [(name, ddl) for name, ddl in usage_required.items() if name not in existing_usage]
        if usage_missing:
            with engine.begin() as connection:
                for name, ddl in usage_missing:
                    connection.execute(text(f"ALTER TABLE usage_records ADD COLUMN {name} {ddl}"))
    if "qa_messages" in table_names:
        existing_qa = {column["name"] for column in inspector.get_columns("qa_messages")}
        qa_required = {
            "reasoning_content": "TEXT DEFAULT ''",
            "selected_file_ids": "JSON",
            "user_id": "VARCHAR(64)",
        }
        qa_missing = [(name, ddl) for name, ddl in qa_required.items() if name not in existing_qa]
        if qa_missing:
            with engine.begin() as connection:
                for name, ddl in qa_missing:
                    connection.execute(text(f"ALTER TABLE qa_messages ADD COLUMN {name} {ddl}"))
    if "qa_threads" in table_names:
        existing_threads = {column["name"] for column in inspector.get_columns("qa_threads")}
        thread_required = {
            "user_id": "VARCHAR(64)",
        }
        thread_missing = [(name, ddl) for name, ddl in thread_required.items() if name not in existing_threads]
        if thread_missing:
            with engine.begin() as connection:
                for name, ddl in thread_missing:
                    connection.execute(text(f"ALTER TABLE qa_threads ADD COLUMN {name} {ddl}"))


def ensure_default_admin() -> None:
    from .models import Project, ProjectFile, Recording, User, UserQuota
    from .utils import new_id

    settings = get_settings()
    session = SessionLocal()
    try:
        admin = session.query(User).filter_by(username=settings.admin_username).first()
        if not admin:
            admin = User(
                id=new_id("user"),
                username=settings.admin_username,
                display_name="管理员",
                password_hash=hash_password(settings.admin_password),
                role="admin",
                status="active",
            )
            session.add(admin)
            session.flush()
            session.add(UserQuota(user_id=admin.id))
        elif not admin.password_hash:
            admin.password_hash = hash_password(settings.admin_password)
        session.query(Project).filter(Project.owner_id.is_(None)).update({"owner_id": admin.id}, synchronize_session=False)
        existing_recording_ids = {
            row.recording_id
            for row in session.query(ProjectFile.recording_id).filter(ProjectFile.recording_id.is_not(None)).all()
        }
        for recording in session.query(Recording).all():
            if recording.id in existing_recording_ids:
                continue
            session.add(
                ProjectFile(
                    id=new_id("file"),
                    project_id=recording.project_id,
                    recording_id=recording.id,
                    created_by_id=admin.id,
                    file_name=recording.file_name,
                    file_type="audio",
                    object_key=recording.object_key,
                    storage_config_id=recording.storage_config_id,
                    storage_provider=recording.storage_provider,
                    storage_bucket_name=recording.storage_bucket_name,
                    storage_endpoint=recording.storage_endpoint,
                    storage_region=recording.storage_region,
                    storage_path_prefix=recording.storage_path_prefix,
                    mime_type=recording.mime_type,
                    extension=recording.extension,
                    file_size_bytes=recording.file_size_bytes,
                    duration_seconds=recording.duration_seconds,
                    status=recording.status,
                )
            )
        session.commit()
    finally:
        session.close()


def hash_password(password: str) -> str:
    return hashlib.sha256(f"ai-asr-file:{password}".encode("utf-8")).hexdigest()


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
