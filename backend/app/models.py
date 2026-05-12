from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    recordings: Mapped[list["Recording"]] = relationship(cascade="all, delete-orphan", back_populates="project")
    files: Mapped[list["ProjectFile"]] = relationship(cascade="all, delete-orphan", back_populates="project")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserQuota(Base):
    __tablename__ = "user_quotas"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    daily_asr_seconds: Mapped[int] = mapped_column(Integer, default=0)
    monthly_asr_seconds: Mapped[int] = mapped_column(Integer, default=0)
    daily_qa_tokens: Mapped[int] = mapped_column(Integer, default=0)
    monthly_qa_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectFile(Base):
    __tablename__ = "project_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    recording_id: Mapped[str | None] = mapped_column(ForeignKey("recordings.id"), nullable=True, index=True)
    created_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(64), default="audio", index=True)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_config_id: Mapped[str] = mapped_column(String(64), default="default")
    storage_provider: Mapped[str] = mapped_column(String(64), default="")
    storage_bucket_name: Mapped[str] = mapped_column(String(255), default="")
    storage_endpoint: Mapped[str] = mapped_column(String(1024), default="")
    storage_region: Mapped[str] = mapped_column(String(128), default="")
    storage_path_prefix: Mapped[str] = mapped_column(String(512), default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(64), default="created", index=True)
    extraction_status: Mapped[str] = mapped_column(String(64), default="")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    extracted_char_count: Mapped[int] = mapped_column(Integer, default=0)
    extraction_engine: Mapped[str] = mapped_column(String(255), default="")
    extraction_warnings: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="files")


class ProjectFileReference(Base):
    __tablename__ = "project_file_references"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_project_id: Mapped[str] = mapped_column(String(64), index=True)
    source_file_id: Mapped[str] = mapped_column(String(64), index=True)
    created_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(64), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_config_id: Mapped[str] = mapped_column(String(64), default="default")
    storage_provider: Mapped[str] = mapped_column(String(64), default="")
    storage_bucket_name: Mapped[str] = mapped_column(String(255), default="")
    storage_endpoint: Mapped[str] = mapped_column(String(1024), default="")
    storage_region: Mapped[str] = mapped_column(String(128), default="")
    storage_path_prefix: Mapped[str] = mapped_column(String(512), default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    extension: Mapped[str] = mapped_column(String(32), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(64), default="created", index=True)
    template_type: Mapped[str] = mapped_column(String(64), default="customer_interview")
    summary_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="recordings")
    jobs: Mapped[list["ProcessingJob"]] = relationship(cascade="all, delete-orphan")
    raw_segments: Mapped[list["RawTranscriptSegment"]] = relationship(cascade="all, delete-orphan")
    clean_segments: Mapped[list["CleanTranscriptSegment"]] = relationship(cascade="all, delete-orphan")
    summary: Mapped["SummaryArtifact"] = relationship(cascade="all, delete-orphan", uselist=False)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    recording_id: Mapped[str | None] = mapped_column(ForeignKey("recordings.id"), nullable=True, index=True)
    file_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RawTranscriptSegment(Base):
    __tablename__ = "raw_transcript_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), index=True)
    speaker: Mapped[str] = mapped_column(String(128), default="说话人")
    start_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class CleanTranscriptSegment(Base):
    __tablename__ = "clean_transcript_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), index=True)
    raw_segment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    speaker: Mapped[str] = mapped_column(String(128), default="说话人")
    start_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SummaryArtifact(Base):
    __tablename__ = "summary_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), unique=True, index=True)
    template_type: Mapped[str] = mapped_column(String(64), default="customer_interview")
    status: Mapped[str] = mapped_column(String(64), default="ready")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QASession(Base):
    __tablename__ = "qa_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(64), default="queued")
    recording_ids: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QAThread(Base):
    __tablename__ = "qa_threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[list["QAMessage"]] = relationship(cascade="all, delete-orphan", back_populates="thread")


class QAMessage(Base):
    __tablename__ = "qa_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("qa_threads.id"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    reasoning_content: Mapped[str] = mapped_column(Text, default="")
    selected_recording_ids: Mapped[list] = mapped_column(JSON, default=list)
    selected_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(64), default="ready")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    thread: Mapped[QAThread] = relationship(back_populates="messages")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    recording_id: Mapped[str | None] = mapped_column(String(64), index=True)
    file_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    call_type: Mapped[str] = mapped_column(String(64), index=True)
    model_provider: Mapped[str] = mapped_column(String(128), default="mock")
    model_name: Mapped[str] = mapped_column(String(255), default="mock")
    audio_duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(64), default="succeeded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_path: Mapped[str] = mapped_column(String(512), default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportFile(Base):
    __tablename__ = "export_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    export_type: Mapped[str] = mapped_column(String(64), default="summary")
    format: Mapped[str] = mapped_column(String(32), default="markdown")
    object_key: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(64), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
