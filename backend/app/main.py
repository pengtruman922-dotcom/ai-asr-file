from pathlib import Path
from datetime import datetime, timezone
import json
import time
import re

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .clients import llm_client
from .config import get_settings
from .database import get_db, init_db, session_scope
from .database import hash_password
from .models import (
    CleanTranscriptSegment,
    ExportFile,
    ProcessingJob,
    ProjectFile,
    ProjectFileReference,
    ProjectMember,
    QAMessage,
    Project,
    QASession,
    QAThread,
    RawTranscriptSegment,
    Recording,
    SummaryArtifact,
    SystemSetting,
    User,
    UserQuota,
    UsageRecord,
)
from .storage import storage
from .settings_service import AI_NODES, get_ai_config, get_app_settings, get_basic_config, public_app_settings, resolve_storage_config, save_app_settings
from .tasks import create_job, enqueue_job, retry_failed_job
from .utils import fail, make_token, new_id, ok, require_auth, serialize_dt

settings = get_settings()
app = FastAPI(title="AI ASR File MVP")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}
DOCUMENT_EXTENSIONS = {"pdf", "xlsx", "xlsm", "xls", "docx", "txt", "md", "markdown"}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | DOCUMENT_EXTENSIONS
FIXED_SPEAKER_COUNTS = {2, 3, 4}
CONTEXT_CHAR_LIMIT = 100000
DEFAULT_USER_QUOTA_SETTING_KEY = "default_user_quota"
QUOTA_FIELDS = ["daily_asr_seconds", "monthly_asr_seconds", "daily_qa_tokens", "monthly_qa_tokens"]


def asr_speaker_metadata(value) -> dict:
    raw = "" if value is None else str(value).strip().lower()
    if raw in {"", "2"}:
        count = 2
    elif raw in {"auto", "smart", "0"}:
        count = 0
    else:
        try:
            count = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("SPEAKER_COUNT_INVALID") from exc
        if count not in FIXED_SPEAKER_COUNTS:
            raise ValueError("SPEAKER_COUNT_INVALID")
    return {
        "asr_speaker_count": count,
        "asr_speaker_mode": "auto" if count == 0 else "fixed",
    }


def file_type_for_extension(extension: str) -> str:
    extension = extension.lower().lstrip(".")
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension == "pdf":
        return "pdf"
    if extension in {"xlsx", "xlsm", "xls"}:
        return "excel"
    if extension == "docx":
        return "docx"
    if extension in {"txt"}:
        return "text"
    if extension in {"md", "markdown"}:
        return "markdown"
    return "unknown"


def current_user(request: Request, db: Session) -> User:
    username = require_auth(request)
    user = db.query(User).filter_by(username=username).first()
    if not user or user.status != "active":
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return user


def require_admin_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
    return user


def user_payload(user: User, db: Session | None = None):
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "status": user.status,
        "created_at": serialize_dt(user.created_at),
        "updated_at": serialize_dt(user.updated_at),
    }


def project_access_role(project: Project, user: User, db: Session) -> str:
    if user.role == "admin":
        return "admin"
    if project.owner_id == user.id:
        return "owner"
    member = db.query(ProjectMember).filter_by(project_id=project.id, user_id=user.id).first()
    if member:
        return "member"
    if project.is_shared:
        return "shared"
    return ""


def ensure_project_access(project_id: str, user: User, db: Session, allow_shared: bool = True) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    role = project_access_role(project, user, db)
    if not role or (role == "shared" and not allow_shared):
        raise HTTPException(status_code=403, detail="PROJECT_FORBIDDEN")
    return project


def ensure_project_owner_or_admin(project: Project, user: User) -> None:
    if user.role != "admin" and project.owner_id != user.id:
        raise HTTPException(status_code=403, detail="PROJECT_OWNER_REQUIRED")


def quota_payload(quota: UserQuota | None):
    return {
        "daily_asr_seconds": quota.daily_asr_seconds if quota else 0,
        "monthly_asr_seconds": quota.monthly_asr_seconds if quota else 0,
        "daily_qa_tokens": quota.daily_qa_tokens if quota else 0,
        "monthly_qa_tokens": quota.monthly_qa_tokens if quota else 0,
    }


def normalize_quota_payload(payload: dict | None) -> dict[str, int]:
    payload = payload or {}
    normalized: dict[str, int] = {}
    for field in QUOTA_FIELDS:
        try:
            normalized[field] = max(0, int(payload.get(field) or 0))
        except (TypeError, ValueError):
            normalized[field] = 0
    return normalized


def default_quota_payload(db: Session) -> dict[str, int]:
    row = db.get(SystemSetting, DEFAULT_USER_QUOTA_SETTING_KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return normalize_quota_payload(value)


def ensure_user_quota(db: Session, user_id: str, defaults: dict[str, int] | None = None) -> UserQuota:
    quota = db.get(UserQuota, user_id)
    if quota:
        return quota
    values = defaults if defaults is not None else default_quota_payload(db)
    quota = UserQuota(user_id=user_id, **normalize_quota_payload(values))
    db.add(quota)
    return quota


def apply_quota_values(quota: UserQuota, values: dict[str, int]) -> None:
    for field, value in normalize_quota_payload(values).items():
        setattr(quota, field, value)


def build_user_usage_payload(db: Session, user: User):
    now = datetime.now(timezone.utc)
    day_prefix = now.date().isoformat()
    month_prefix = now.strftime("%Y-%m")
    rows = db.query(UsageRecord).filter_by(user_id=user.id).all()

    def in_day(row: UsageRecord) -> bool:
        return serialize_dt(row.created_at).startswith(day_prefix) if row.created_at else False

    def in_month(row: UsageRecord) -> bool:
        return serialize_dt(row.created_at).startswith(month_prefix) if row.created_at else False

    today_rows = [row for row in rows if in_day(row)]
    month_rows = [row for row in rows if in_month(row)]
    quota = db.get(UserQuota, user.id)
    return {
        "user": user_payload(user, db),
        "quota": quota_payload(quota),
        "today": {
            "asr_seconds": sum(row.audio_duration_seconds or 0 for row in today_rows if row.call_type == "asr"),
            "qa_tokens": sum((row.input_tokens or 0) + (row.output_tokens or 0) for row in today_rows if row.call_type == "qa"),
        },
        "month": {
            "asr_seconds": sum(row.audio_duration_seconds or 0 for row in month_rows if row.call_type == "asr"),
            "qa_tokens": sum((row.input_tokens or 0) + (row.output_tokens or 0) for row in month_rows if row.call_type == "qa"),
        },
    }


def quota_exceeded(db: Session, user: User, usage_type: str, add_value: int) -> tuple[bool, str]:
    if user.role == "admin":
        return False, ""
    usage = build_user_usage_payload(db, user)
    quota = usage["quota"]
    if usage_type == "asr":
        if quota["daily_asr_seconds"] and usage["today"]["asr_seconds"] + add_value > quota["daily_asr_seconds"]:
            return True, "今日 ASR 时长额度不足"
        if quota["monthly_asr_seconds"] and usage["month"]["asr_seconds"] + add_value > quota["monthly_asr_seconds"]:
            return True, "本月 ASR 时长额度不足"
    if usage_type == "qa":
        if quota["daily_qa_tokens"] and usage["today"]["qa_tokens"] + add_value > quota["daily_qa_tokens"]:
            return True, "今日问答 Token 额度不足"
        if quota["monthly_qa_tokens"] and usage["month"]["qa_tokens"] + add_value > quota["monthly_qa_tokens"]:
            return True, "本月问答 Token 额度不足"
    return False, ""


@app.on_event("startup")
def startup():
    init_db()
    settings.local_storage_path.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api"):
        try:
            require_auth(request)
        except HTTPException:
            return fail("UNAUTHORIZED", "请先登录", status_code=401)
    return await call_next(request)


def project_payload(project: Project, db: Session, user: User | None = None):
    files = db.query(ProjectFile).filter_by(project_id=project.id).all()
    recs = db.query(Recording).filter_by(project_id=project.id).all()
    owner = db.get(User, project.owner_id) if project.owner_id else None
    access_role = project_access_role(project, user, db) if user else ""
    return {
        "project_id": project.id,
        "title": project.title,
        "description": project.description,
        "owner_id": project.owner_id,
        "owner_name": owner.display_name or owner.username if owner else "",
        "is_shared": project.is_shared,
        "access_role": access_role,
        "recording_count": len(files) if files else len(recs),
        "file_count": len(files) if files else len(recs),
        "total_duration_seconds": sum(r.duration_seconds or 0 for r in recs),
        "created_at": serialize_dt(project.created_at),
        "updated_at": serialize_dt(project.updated_at),
    }


def recording_payload(recording: Recording, db: Session | None = None):
    current_job = None
    latest_failed_job = None
    if db and recording.status not in {"created", "completed", "failed"}:
        current_job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.recording_id == recording.id, ProcessingJob.status.in_(["queued", "running"]))
            .order_by(ProcessingJob.created_at.desc())
            .first()
        )
    if db and recording.status == "failed":
        latest_failed_job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.recording_id == recording.id, ProcessingJob.status == "failed")
            .order_by(ProcessingJob.finished_at.desc().nullslast(), ProcessingJob.updated_at.desc())
            .first()
        )
    payload = {
        "recording_id": recording.id,
        "project_id": recording.project_id,
        "file_name": recording.file_name,
        "object_key": recording.object_key,
        "storage": {
            "provider": recording.storage_provider,
            "bucket_name": recording.storage_bucket_name,
            "endpoint": recording.storage_endpoint,
            "region": recording.storage_region,
            "path_prefix": recording.storage_path_prefix,
        },
        "mime_type": recording.mime_type,
        "extension": recording.extension,
        "file_size_bytes": recording.file_size_bytes,
        "duration_seconds": recording.duration_seconds,
        "status": recording.status,
        "template_type": recording.template_type,
        "summary_stale": recording.summary_stale,
        "created_at": serialize_dt(recording.created_at),
        "updated_at": serialize_dt(recording.updated_at),
    }
    if current_job:
        payload.update(
            {
                "current_job_type": current_job.job_type,
                "current_job_status": current_job.status,
                "current_job_created_at": serialize_dt(current_job.created_at),
                "current_job_started_at": serialize_dt(current_job.started_at),
            }
        )
    if latest_failed_job:
        payload.update(
            {
                "latest_failed_job_id": latest_failed_job.id,
                "latest_failed_job_type": latest_failed_job.job_type,
                "latest_failed_job_error_code": latest_failed_job.error_code,
                "latest_failed_job_error_message": latest_failed_job.error_message,
                "latest_failed_job_finished_at": serialize_dt(latest_failed_job.finished_at),
            }
        )
    return payload


def file_payload(project_file: ProjectFile, db: Session | None = None, reference: ProjectFileReference | None = None):
    status = project_file.status
    if project_file.recording_id and db:
        recording = db.get(Recording, project_file.recording_id)
        if recording:
            status = recording.status
            project_file.duration_seconds = recording.duration_seconds or project_file.duration_seconds
    current_job = None
    latest_failed_job = None
    if db and status not in {"created", "completed", "failed"}:
        current_job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.file_id == project_file.id, ProcessingJob.status.in_(["queued", "running"]))
            .order_by(ProcessingJob.created_at.desc())
            .first()
        )
    if db and status == "failed":
        latest_failed_job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.file_id == project_file.id, ProcessingJob.status == "failed")
            .order_by(ProcessingJob.finished_at.desc().nullslast(), ProcessingJob.updated_at.desc())
            .first()
        )
    payload = {
        "file_id": project_file.id,
        "recording_id": project_file.recording_id or project_file.id,
        "project_id": reference.target_project_id if reference else project_file.project_id,
        "source_project_id": project_file.project_id,
        "reference_id": reference.id if reference else None,
        "reference_status": reference.status if reference else "",
        "source": "reference" if reference else "own",
        "file_name": project_file.file_name,
        "file_type": project_file.file_type,
        "object_key": project_file.object_key,
        "mime_type": project_file.mime_type,
        "extension": project_file.extension,
        "file_size_bytes": project_file.file_size_bytes,
        "duration_seconds": project_file.duration_seconds,
        "status": status,
        "extraction_status": project_file.extraction_status,
        "extracted_char_count": project_file.extracted_char_count,
        "extraction_engine": project_file.extraction_engine,
        "extraction_warnings": project_file.extraction_warnings or [],
        "created_at": serialize_dt(project_file.created_at),
        "updated_at": serialize_dt(project_file.updated_at),
    }
    if current_job:
        payload.update(
            {
                "current_job_type": current_job.job_type,
                "current_job_status": current_job.status,
                "current_job_created_at": serialize_dt(current_job.created_at),
                "current_job_started_at": serialize_dt(current_job.started_at),
            }
        )
    if latest_failed_job:
        payload.update(
            {
                "latest_failed_job_id": latest_failed_job.id,
                "latest_failed_job_type": latest_failed_job.job_type,
                "latest_failed_job_error_code": latest_failed_job.error_code,
                "latest_failed_job_error_message": latest_failed_job.error_message,
                "latest_failed_job_finished_at": serialize_dt(latest_failed_job.finished_at),
            }
        )
    return payload


def job_payload(job: ProcessingJob):
    return {
        "job_id": job.id,
        "project_id": job.project_id,
        "recording_id": job.recording_id,
        "file_id": job.file_id,
        "user_id": job.user_id,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "external_task_id": job.external_task_id,
        "metadata": job.metadata_json,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": serialize_dt(job.created_at),
        "started_at": serialize_dt(job.started_at),
        "finished_at": serialize_dt(job.finished_at),
    }


@app.get("/api/health")
def health():
    return ok({"status": "ok", "version": "0.1.0", "services": {"database": "ok", "storage": "ok"}})


@app.post("/api/auth/login")
async def login(payload: dict, db: Session = Depends(get_db)):
    username = str(payload.get("username") or "").strip()
    user = db.query(User).filter_by(username=username).first()
    if user and user.status == "active" and user.password_hash == hash_password(str(payload.get("password") or "")):
        return ok({"token": make_token(user.username), "user": user_payload(user, db), "username": user.username})
    return fail("UNAUTHORIZED", "账号或密码错误", status_code=401)


@app.get("/api/auth/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    return ok(user_payload(user, db))


@app.post("/api/auth/logout")
def logout():
    return ok({})


@app.get("/api/me/usage")
def my_usage(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    return ok(build_user_usage_payload(db, user))


@app.get("/api/users/search")
def search_users(
    request: Request,
    keyword: str = "",
    project_id: str = "",
    limit: int = 20,
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    keyword = keyword.strip()
    limit = max(1, min(limit, 50))
    query = db.query(User).filter(User.status == "active")
    if keyword:
        pattern = f"%{keyword}%"
        query = query.filter(or_(User.username.ilike(pattern), User.display_name.ilike(pattern)))
    elif project_id:
        return ok({"items": []})
    if project_id:
        project = db.get(Project, project_id)
        if not project:
            return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
        ensure_project_owner_or_admin(project, user)
        member_user_ids = [row.user_id for row in db.query(ProjectMember.user_id).filter_by(project_id=project_id).all()]
        if member_user_ids:
            query = query.filter(~User.id.in_(member_user_ids))
    rows = query.order_by(User.username.asc()).limit(limit).all()
    return ok({"items": [user_payload(row, db) for row in rows]})


@app.get("/api/admin/users")
def admin_list_users(request: Request, include_deleted: bool = False, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    query = db.query(User)
    if not include_deleted:
        query = query.filter(User.status != "deleted")
    rows = query.order_by(User.created_at.desc()).all()
    return ok({"items": [user_payload(row, db) | {"quota": quota_payload(db.get(UserQuota, row.id))} for row in rows]})


@app.get("/api/admin/default-quota")
def admin_get_default_quota(request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    return ok(default_quota_payload(db))


@app.patch("/api/admin/default-quota")
async def admin_patch_default_quota(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    values = normalize_quota_payload(payload)
    row = db.get(SystemSetting, DEFAULT_USER_QUOTA_SETTING_KEY)
    if not row:
        row = SystemSetting(key=DEFAULT_USER_QUOTA_SETTING_KEY, value=values)
        db.add(row)
    else:
        row.value = values
    updated_count = 0
    users = db.query(User).filter(User.status != "deleted").all()
    for user in users:
        quota = ensure_user_quota(db, user.id, values)
        apply_quota_values(quota, values)
        updated_count += 1
    db.commit()
    return ok(values | {"updated_user_count": updated_count})


@app.post("/api/admin/users")
async def admin_create_user(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not username or not password:
        return fail("VALIDATION_ERROR", "用户ID和初始密码不能为空")
    if db.query(User).filter_by(username=username).first():
        return fail("USER_EXISTS", "用户ID已存在")
    default_quota = default_quota_payload(db)
    user = User(
        id=new_id("user"),
        username=username,
        display_name=str(payload.get("display_name") or username).strip(),
        password_hash=hash_password(password),
        role=payload.get("role") if payload.get("role") in {"admin", "user"} else "user",
        status=payload.get("status") if payload.get("status") in {"active", "disabled"} else "active",
    )
    db.add(user)
    db.flush()
    db.add(UserQuota(user_id=user.id, **default_quota))
    db.commit()
    return ok(user_payload(user, db))


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    user = db.get(User, user_id)
    if not user:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    if "display_name" in payload:
        user.display_name = str(payload.get("display_name") or "").strip() or user.username
    if payload.get("role") in {"admin", "user"}:
        user.role = payload["role"]
    if payload.get("status") in {"active", "disabled", "deleted"}:
        if user.id == admin.id and payload["status"] in {"disabled", "deleted"}:
            return fail("VALIDATION_ERROR", "不能停用或删除当前登录管理员")
        user.status = payload["status"]
    db.commit()
    return ok(user_payload(user, db))


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    user = db.get(User, user_id)
    if not user:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    if user.id == admin.id:
        return fail("VALIDATION_ERROR", "不能删除当前登录管理员")
    user.status = "deleted"
    db.commit()
    return ok({"deleted": True, "user": user_payload(user, db)})


@app.post("/api/admin/users/batch-delete")
async def admin_batch_delete_users(payload: dict, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    raw_ids = payload.get("user_ids") if isinstance(payload, dict) else []
    if not isinstance(raw_ids, list):
        return fail("VALIDATION_ERROR", "请选择要删除的用户")
    user_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    deleted_count = 0
    skipped_user_ids: list[str] = []
    for user_id in user_ids:
        user = db.get(User, user_id)
        if not user or user.id == admin.id or user.status == "deleted":
            skipped_user_ids.append(user_id)
            continue
        user.status = "deleted"
        deleted_count += 1
    db.commit()
    return ok({"deleted_count": deleted_count, "skipped_user_ids": skipped_user_ids})


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    user = db.get(User, user_id)
    if not user:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    password = str(payload.get("password") or "").strip()
    if not password:
        return fail("VALIDATION_ERROR", "新密码不能为空")
    user.password_hash = hash_password(password)
    db.commit()
    return ok({"reset": True})


@app.get("/api/admin/users/{user_id}/quota")
def admin_get_quota(user_id: str, request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    user = db.get(User, user_id)
    if not user:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    quota = ensure_user_quota(db, user_id)
    db.commit()
    return ok(quota_payload(quota))


@app.patch("/api/admin/users/{user_id}/quota")
async def admin_patch_quota(user_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin_user(request, db)
    user = db.get(User, user_id)
    if not user:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    quota = db.get(UserQuota, user_id)
    if not quota:
        quota = ensure_user_quota(db, user_id)
    apply_quota_values(quota, payload)
    db.commit()
    return ok(quota_payload(quota))


@app.put("/api/mock-storage/{object_key:path}")
async def mock_upload(object_key: str, request: Request):
    storage.save_local_bytes(object_key, await request.body())
    return PlainTextResponse("ok")


@app.get("/api/mock-storage/{object_key:path}")
def mock_download(object_key: str):
    path = storage._local_path(object_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@app.post("/api/projects")
async def create_project(payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    title = (payload.get("title") or "").strip()
    if not title:
        return fail("VALIDATION_ERROR", "项目名称不能为空")
    project = Project(id=new_id("proj"), title=title, description=payload.get("description", "") or "", owner_id=user.id)
    db.add(project)
    db.flush()
    db.add(ProjectMember(id=new_id("pm"), project_id=project.id, user_id=user.id))
    db.commit()
    db.refresh(project)
    return ok(project_payload(project, db, user))


@app.get("/api/projects")
def list_projects(request: Request, keyword: str = "", page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    user = current_user(request, db)
    query = db.query(Project)
    if user.role != "admin":
        member_project_ids = [row.project_id for row in db.query(ProjectMember.project_id).filter_by(user_id=user.id).all()]
        query = query.filter((Project.owner_id == user.id) | (Project.id.in_(member_project_ids)) | (Project.is_shared.is_(True)))
    if keyword:
        query = query.filter(Project.title.contains(keyword))
    total = query.count()
    items = query.order_by(Project.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [project_payload(item, db, user) for item in items], "page": page, "page_size": page_size, "total": total})


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        project = ensure_project_access(project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    payload = project_payload(project, db, user)
    payload["stats"] = dict(db.query(Recording.status, func.count(Recording.id)).filter_by(project_id=project_id).group_by(Recording.status).all())
    return ok(payload)


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        project = ensure_project_access(project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    if "title" in payload or "description" in payload:
        if project.owner_id != user.id and user.role != "admin":
            return fail("PROJECT_OWNER_REQUIRED", "只有项目创建人或管理员可以修改项目基础信息", status_code=403)
    project.title = payload.get("title", project.title)
    project.description = payload.get("description", project.description)
    db.commit()
    return ok(project_payload(project, db, user))


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, request: Request, force: bool = False, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    if project.owner_id != user.id and user.role != "admin":
        return fail("PROJECT_OWNER_REQUIRED", "只有项目创建人或管理员可以删除项目", status_code=403)
    reference_count = db.query(ProjectFileReference).filter_by(source_project_id=project_id, status="active").count()
    if reference_count and not force:
        return fail("PROJECT_HAS_REFERENCES", "该项目存在被其他项目引用的文件，请二次确认后再删除", details={"reference_count": reference_count})
    db.query(ProjectFileReference).filter_by(source_project_id=project_id).update({"status": "source_deleted"}, synchronize_session=False)
    for recording in db.query(Recording).filter_by(project_id=project_id).all():
        storage.delete_prefix(f"projects/{project_id}/recordings/{recording.id}/", storage.recording_config(recording))
    for project_file in db.query(ProjectFile).filter_by(project_id=project_id).all():
        storage.delete_prefix(f"projects/{project_id}/files/{project_file.id}/", storage.file_config(project_file))
    storage.delete_prefix(f"projects/{project_id}/")
    db.query(ProjectFileReference).filter_by(target_project_id=project_id).delete(synchronize_session=False)
    db.query(ProjectMember).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(UsageRecord).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QASession).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QAMessage).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QAThread).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ExportFile).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ProcessingJob).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.delete(project)
    db.commit()
    return ok({"deleted": True})


@app.get("/api/projects/{project_id}/members")
def list_project_members(project_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        project = ensure_project_access(project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    rows = db.query(ProjectMember).filter_by(project_id=project.id).all()
    users = [db.get(User, row.user_id) for row in rows]
    return ok({"items": [user_payload(item, db) for item in users if item], "owner_id": project.owner_id})


@app.post("/api/projects/{project_id}/members")
async def add_project_member(project_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    ensure_project_owner_or_admin(project, user)
    target_username = str(payload.get("username") or "").strip()
    target_user_id = str(payload.get("user_id") or "").strip()
    target = db.get(User, target_user_id) if target_user_id else db.query(User).filter_by(username=target_username).first()
    if not target:
        return fail("USER_NOT_FOUND", "用户不存在", status_code=404)
    if target.status != "active":
        return fail("USER_DISABLED", "只能添加启用状态的用户", status_code=400)
    if not db.query(ProjectMember).filter_by(project_id=project.id, user_id=target.id).first():
        db.add(ProjectMember(id=new_id("pm"), project_id=project.id, user_id=target.id))
        db.commit()
    return ok({"added": True, "user": user_payload(target, db)})


@app.delete("/api/projects/{project_id}/members/{user_id}")
def remove_project_member(project_id: str, user_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    ensure_project_owner_or_admin(project, user)
    if user_id == project.owner_id:
        return fail("VALIDATION_ERROR", "不能移除项目创建人")
    db.query(ProjectMember).filter_by(project_id=project.id, user_id=user_id).delete(synchronize_session=False)
    db.commit()
    return ok({"removed": True})


@app.patch("/api/projects/{project_id}/sharing")
async def patch_project_sharing(project_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    ensure_project_owner_or_admin(project, user)
    next_shared = bool(payload.get("is_shared"))
    if project.is_shared and not next_shared:
        reference_count = db.query(ProjectFileReference).filter_by(source_project_id=project.id, status="active").count()
        if reference_count and not payload.get("force"):
            return fail("PROJECT_HAS_REFERENCES", "该项目存在被引用文件，请二次确认后再取消共享", details={"reference_count": reference_count})
        db.query(ProjectFileReference).filter_by(source_project_id=project.id, status="active").update({"status": "source_unshared"}, synchronize_session=False)
    project.is_shared = next_shared
    db.commit()
    return ok(project_payload(project, db, user) | {"reference_count": db.query(ProjectFileReference).filter_by(source_project_id=project.id).count()})


@app.get("/api/projects/{project_id}/references/check")
def check_project_references(project_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    ensure_project_owner_or_admin(project, user)
    rows = db.query(ProjectFileReference).filter_by(source_project_id=project_id, status="active").all()
    return ok({"reference_count": len(rows), "items": [{"reference_id": row.id, "target_project_id": row.target_project_id, "source_file_id": row.source_file_id} for row in rows]})


@app.post("/api/projects/{project_id}/files/upload-session")
async def create_file_upload_session(project_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        project = ensure_project_access(project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    extension = (payload.get("extension") or payload.get("file_name", "").rsplit(".", 1)[-1]).lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        return fail("UNSUPPORTED_FILE_TYPE", "文件格式不支持")
    file_type = file_type_for_extension(extension)
    size = int(payload.get("file_size_bytes") or 0)
    basic_config = get_basic_config(db)
    max_size_mb = basic_config.get("max_upload_size_mb", settings.max_upload_size_mb)
    if size > max_size_mb * 1024 * 1024:
        return fail("FILE_TOO_LARGE", f"文件超过 {max_size_mb}M")
    duration_seconds = int(payload.get("duration_seconds") or 0)
    if file_type == "audio":
        max_duration_hours = basic_config.get("max_recording_duration_hours", settings.max_recording_duration_hours)
        if duration_seconds and duration_seconds > max_duration_hours * 3600:
            return fail("FILE_TOO_LONG", f"文件时长超过 {max_duration_hours} 小时")
        exceeded, reason = quota_exceeded(db, user, "asr", duration_seconds or 0)
        if exceeded:
            return fail("USER_QUOTA_EXCEEDED", reason)
        try:
            asr_speaker_metadata(payload.get("speaker_count"))
        except ValueError:
            return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")

    file_id = new_id("file")
    recording_id = new_id("rec") if file_type == "audio" else None
    storage_config = resolve_storage_config(db)
    object_key = f"projects/{project_id}/files/{file_id}/original.{extension}"
    if file_type == "audio":
        recording = Recording(
            id=recording_id,
            project_id=project.id,
            file_name=payload.get("file_name") or f"{file_id}.{extension}",
            object_key=object_key,
            storage_config_id="default",
            storage_provider=storage_config.get("provider", "railway_bucket"),
            storage_bucket_name=storage_config.get("bucket_name", ""),
            storage_endpoint=storage_config.get("endpoint", ""),
            storage_region=storage_config.get("region", "auto"),
            storage_path_prefix=storage_config.get("path_prefix", ""),
            mime_type=payload.get("mime_type") or "application/octet-stream",
            extension=extension,
            file_size_bytes=size,
            duration_seconds=duration_seconds,
            status="uploading",
            template_type=payload.get("template_type") or "customer_interview",
        )
        db.add(recording)
        # PostgreSQL enforces the project_files.recording_id foreign key immediately.
        # Flush the recording first so the unified file row can safely reference it.
        db.flush()
    project_file = ProjectFile(
        id=file_id,
        project_id=project.id,
        recording_id=recording_id,
        created_by_id=user.id,
        file_name=payload.get("file_name") or f"{file_id}.{extension}",
        file_type=file_type,
        object_key=object_key,
        storage_config_id="default",
        storage_provider=storage_config.get("provider", "railway_bucket"),
        storage_bucket_name=storage_config.get("bucket_name", ""),
        storage_endpoint=storage_config.get("endpoint", ""),
        storage_region=storage_config.get("region", "auto"),
        storage_path_prefix=storage_config.get("path_prefix", ""),
        mime_type=payload.get("mime_type") or "application/octet-stream",
        extension=extension,
        file_size_bytes=size,
        duration_seconds=duration_seconds,
        status="uploading",
        extraction_status="" if file_type == "audio" else "pending",
    )
    db.add(project_file)
    try:
        upload = storage.create_upload_url(object_key, project_file.mime_type, storage_config)
    except RuntimeError as exc:
        db.rollback()
        return fail("STORAGE_CONFIG_MISSING", str(exc))
    db.commit()
    return ok({"file_id": file_id, "recording_id": recording_id, "object_key": object_key, "file_type": file_type, "upload": upload})


@app.post("/api/files/{file_id}/upload-content")
def upload_file_content(file_id: str, request: Request, file: UploadFile = File(...), speaker_count: str | None = Form(None), db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    try:
        ensure_project_access(project_file.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    basic_config = get_basic_config(db)
    max_size_mb = basic_config.get("max_upload_size_mb", settings.max_upload_size_mb)
    if size > max_size_mb * 1024 * 1024:
        project_file.status = "failed"
        db.commit()
        return fail("FILE_TOO_LARGE", f"文件超过 {max_size_mb}M")
    try:
        storage.upload_fileobj(project_file.object_key, file.file, file.content_type or project_file.mime_type, storage.file_config(project_file))
    except RuntimeError as exc:
        project_file.status = "failed"
        db.commit()
        return fail("STORAGE_CONFIG_MISSING", str(exc))
    except Exception as exc:
        project_file.status = "failed"
        db.commit()
        return fail("STORAGE_UPLOAD_FAILED", f"文件写入 Bucket 失败：{exc}")

    project_file.file_size_bytes = size
    project_file.mime_type = file.content_type or project_file.mime_type
    project_file.status = "queued"
    if project_file.file_type == "audio":
        try:
            metadata = asr_speaker_metadata(speaker_count)
        except ValueError:
            return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")
        metadata.update({"file_id": project_file.id, "user_id": user.id})
        recording = db.get(Recording, project_file.recording_id)
        if recording:
            recording.file_size_bytes = size
            recording.mime_type = project_file.mime_type
            recording.status = "queued"
        job = create_job(db, project_file.project_id, project_file.recording_id, "asr_transcription", metadata)
    else:
        project_file.extraction_status = "queued"
        job = create_job(db, project_file.project_id, None, "extract_text", {"file_id": project_file.id, "user_id": user.id})
    db.commit()
    enqueue_job(job.id)
    return ok({"file_id": project_file.id, "recording_id": project_file.recording_id, "status": "queued", "job_id": job.id})


@app.get("/api/projects/{project_id}/files")
def list_project_files(project_id: str, request: Request, keyword: str = "", page: int = 1, page_size: int = 100, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    query = db.query(ProjectFile).filter_by(project_id=project_id)
    if keyword:
        query = query.filter(ProjectFile.file_name.contains(keyword))
    own_files = query.order_by(ProjectFile.created_at.desc()).all()
    references = db.query(ProjectFileReference).filter_by(target_project_id=project_id).order_by(ProjectFileReference.created_at.desc()).all()
    items = [file_payload(item, db) for item in own_files]
    for reference in references:
        source_file = db.get(ProjectFile, reference.source_file_id)
        if source_file:
            items.append(file_payload(source_file, db, reference))
        else:
            reference.status = "file_deleted"
    db.commit()
    total = len(items)
    offset = (page - 1) * page_size
    return ok({"items": items[offset: offset + page_size], "page": page, "page_size": page_size, "total": total})


@app.get("/api/files/{file_id}")
def get_file(file_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    try:
        ensure_project_access(project_file.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "文件不存在或无权访问", status_code=exc.status_code)
    return ok(file_payload(project_file, db))


@app.get("/api/files/{file_id}/extracted-text")
def get_file_extracted_text(file_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    try:
        ensure_project_access(project_file.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "文件不存在或无权访问", status_code=exc.status_code)
    return ok(file_payload(project_file, db) | {"extracted_text": project_file.extracted_text or ""})


@app.patch("/api/files/{file_id}")
async def update_file(file_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    try:
        ensure_project_access(project_file.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "文件不存在或无权访问", status_code=exc.status_code)
    file_name = str(payload.get("file_name") or "").strip()
    if not file_name:
        return fail("VALIDATION_ERROR", "文件名称不能为空")
    project_file.file_name = file_name
    if project_file.recording_id:
        recording = db.get(Recording, project_file.recording_id)
        if recording:
            recording.file_name = file_name
    db.commit()
    return ok(file_payload(project_file, db))


@app.post("/api/files/{file_id}/reprocess")
def reprocess_file(file_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    try:
        ensure_project_access(project_file.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "文件不存在或无权访问", status_code=exc.status_code)
    if project_file.file_type == "audio":
        job = create_job(db, project_file.project_id, project_file.recording_id, "asr_transcription", {"file_id": project_file.id, "user_id": user.id})
        project_file.status = "queued"
        recording = db.get(Recording, project_file.recording_id)
        if recording:
            recording.status = "queued"
    else:
        job = create_job(db, project_file.project_id, None, "extract_text", {"file_id": project_file.id, "user_id": user.id})
        project_file.status = "queued"
        project_file.extraction_status = "queued"
    db.commit()
    enqueue_job(job.id)
    return ok({"job_id": job.id, "status": "queued"})


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str, request: Request, force: bool = False, db: Session = Depends(get_db)):
    user = current_user(request, db)
    project_file = db.get(ProjectFile, file_id)
    if not project_file:
        return fail("FILE_NOT_FOUND", "文件不存在", status_code=404)
    project = db.get(Project, project_file.project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    if user.role != "admin" and project.owner_id != user.id and project_file.created_by_id != user.id:
        return fail("FILE_DELETE_FORBIDDEN", "只有项目创建人、文件上传人或管理员可以删除文件", status_code=403)
    reference_count = db.query(ProjectFileReference).filter_by(source_file_id=file_id, status="active").count()
    if reference_count and not force:
        return fail("FILE_HAS_REFERENCES", "该文件已被其他项目引用，请二次确认后再删除", details={"reference_count": reference_count})
    db.query(ProjectFileReference).filter_by(source_file_id=file_id).update({"status": "file_deleted"}, synchronize_session=False)
    storage.delete_prefix(f"projects/{project_file.project_id}/files/{project_file.id}/", storage.file_config(project_file))
    if project_file.recording_id:
        recording = db.get(Recording, project_file.recording_id)
        if recording:
            db.delete(recording)
    db.query(UsageRecord).filter_by(file_id=file_id).delete(synchronize_session=False)
    db.query(ProcessingJob).filter_by(file_id=file_id).delete(synchronize_session=False)
    db.delete(project_file)
    db.commit()
    return ok({"deleted": True})


@app.get("/api/shared-files/search")
def search_shared_files(request: Request, keyword: str = "", target_project_id: str = "", db: Session = Depends(get_db)):
    user = current_user(request, db)
    query = db.query(ProjectFile).join(Project, Project.id == ProjectFile.project_id).filter(Project.is_shared.is_(True))
    if target_project_id:
        query = query.filter(ProjectFile.project_id != target_project_id)
    if keyword:
        query = query.filter(ProjectFile.file_name.contains(keyword))
    rows = query.order_by(ProjectFile.updated_at.desc()).limit(50).all()
    return ok({"items": [file_payload(row, db) | {"project_title": db.get(Project, row.project_id).title if db.get(Project, row.project_id) else ""} for row in rows]})


@app.post("/api/projects/{project_id}/file-references")
async def create_file_reference(project_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    source_file_id = str(payload.get("source_file_id") or payload.get("file_id") or "").strip()
    source_file = db.get(ProjectFile, source_file_id)
    if not source_file:
        return fail("FILE_NOT_FOUND", "源文件不存在", status_code=404)
    source_project = db.get(Project, source_file.project_id)
    if not source_project or not source_project.is_shared:
        return fail("SOURCE_PROJECT_NOT_SHARED", "源项目未开启共享")
    existing = db.query(ProjectFileReference).filter_by(target_project_id=project_id, source_file_id=source_file_id).first()
    if existing:
        existing.status = "active"
        db.commit()
        return ok({"reference_id": existing.id, "status": existing.status})
    reference = ProjectFileReference(id=new_id("ref"), target_project_id=project_id, source_project_id=source_file.project_id, source_file_id=source_file.id, created_by_id=user.id)
    db.add(reference)
    db.commit()
    return ok({"reference_id": reference.id, "status": reference.status})


@app.delete("/api/projects/{project_id}/file-references/{reference_id}")
def delete_file_reference(project_id: str, reference_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    db.query(ProjectFileReference).filter_by(id=reference_id, target_project_id=project_id).delete(synchronize_session=False)
    db.commit()
    return ok({"deleted": True})


@app.post("/api/projects/{project_id}/recordings/upload-session")
async def create_upload_session(project_id: str, payload: dict, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    extension = (payload.get("extension") or payload.get("file_name", "").rsplit(".", 1)[-1]).lower()
    if extension not in ALLOWED_EXTENSIONS:
        return fail("UNSUPPORTED_FILE_TYPE", "文件格式不支持")
    size = int(payload.get("file_size_bytes") or 0)
    basic_config = get_basic_config(db)
    max_size_mb = basic_config.get("max_upload_size_mb", settings.max_upload_size_mb)
    if size > max_size_mb * 1024 * 1024:
        return fail("FILE_TOO_LARGE", f"文件超过 {max_size_mb}M")
    duration_seconds = int(payload.get("duration_seconds") or 0)
    max_duration_hours = basic_config.get("max_recording_duration_hours", settings.max_recording_duration_hours)
    if duration_seconds and duration_seconds > max_duration_hours * 3600:
        return fail("FILE_TOO_LONG", f"文件时长超过 {max_duration_hours} 小时")
    try:
        asr_speaker_metadata(payload.get("speaker_count"))
    except ValueError:
        return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")
    recording_id = new_id("rec")
    storage_config = resolve_storage_config(db)
    object_key = f"projects/{project_id}/recordings/{recording_id}/original.{extension}"
    recording = Recording(
        id=recording_id,
        project_id=project_id,
        file_name=payload.get("file_name") or f"{recording_id}.{extension}",
        object_key=object_key,
        storage_config_id="default",
        storage_provider=storage_config.get("provider", "local"),
        storage_bucket_name=storage_config.get("bucket_name", ""),
        storage_endpoint=storage_config.get("endpoint", ""),
        storage_region=storage_config.get("region", "auto"),
        storage_path_prefix=storage_config.get("path_prefix", ""),
        mime_type=payload.get("mime_type") or "application/octet-stream",
        extension=extension,
        file_size_bytes=size,
        duration_seconds=duration_seconds,
        status="uploading",
        template_type="customer_interview",
    )
    db.add(recording)
    try:
        upload = storage.create_upload_url(object_key, recording.mime_type, storage_config)
    except RuntimeError as exc:
        db.rollback()
        return fail("STORAGE_CONFIG_MISSING", str(exc))
    db.commit()
    return ok({"recording_id": recording_id, "object_key": object_key, "upload": upload})


@app.post("/api/recordings/{recording_id}/upload-complete")
async def upload_complete(recording_id: str, payload: dict, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    recording.status = "queued"
    recording.file_size_bytes = int(payload.get("file_size_bytes") or recording.file_size_bytes)
    try:
        metadata = asr_speaker_metadata(payload.get("speaker_count"))
    except ValueError:
        return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")
    job = create_job(db, recording.project_id, recording.id, "asr_transcription", metadata)
    db.commit()
    enqueue_job(job.id)
    return ok({"recording_id": recording.id, "status": "queued", "job_id": job.id})


@app.post("/api/recordings/{recording_id}/upload-content")
def upload_recording_content(recording_id: str, file: UploadFile = File(...), speaker_count: str | None = Form(None), db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    basic_config = get_basic_config(db)
    max_size_mb = basic_config.get("max_upload_size_mb", settings.max_upload_size_mb)
    if size > max_size_mb * 1024 * 1024:
        recording.status = "failed"
        db.commit()
        return fail("FILE_TOO_LARGE", f"文件超过 {max_size_mb}M")
    try:
        metadata = asr_speaker_metadata(speaker_count)
    except ValueError:
        return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")

    try:
        storage.upload_fileobj(recording.object_key, file.file, file.content_type or recording.mime_type, storage.recording_config(recording))
    except RuntimeError as exc:
        recording.status = "failed"
        db.commit()
        return fail("STORAGE_CONFIG_MISSING", str(exc))
    except Exception as exc:
        recording.status = "failed"
        db.commit()
        return fail("STORAGE_UPLOAD_FAILED", f"文件写入 Bucket 失败：{exc}")

    recording.status = "queued"
    recording.file_size_bytes = size
    recording.mime_type = file.content_type or recording.mime_type
    job = create_job(db, recording.project_id, recording.id, "asr_transcription", metadata)
    db.commit()
    enqueue_job(job.id)
    return ok({"recording_id": recording.id, "status": "queued", "job_id": job.id})


@app.post("/api/projects/{project_id}/recordings/upload")
def upload_recording_proxy(
    project_id: str,
    file: UploadFile = File(...),
    duration_seconds: int = Form(0),
    template_type: str = Form("customer_interview"),
    speaker_count: str | None = Form(None),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)

    file_name = file.filename or "recording"
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if extension not in ALLOWED_EXTENSIONS:
        return fail("UNSUPPORTED_FILE_TYPE", "文件格式不支持")

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    basic_config = get_basic_config(db)
    max_size_mb = basic_config.get("max_upload_size_mb", settings.max_upload_size_mb)
    if size > max_size_mb * 1024 * 1024:
        return fail("FILE_TOO_LARGE", f"文件超过 {max_size_mb}M")
    max_duration_hours = basic_config.get("max_recording_duration_hours", settings.max_recording_duration_hours)
    if duration_seconds and duration_seconds > max_duration_hours * 3600:
        return fail("FILE_TOO_LONG", f"文件时长超过 {max_duration_hours} 小时")
    try:
        metadata = asr_speaker_metadata(speaker_count)
    except ValueError:
        return fail("VALIDATION_ERROR", "说话人数量仅支持 2、3、4 或智能识别")

    recording_id = new_id("rec")
    storage_config = resolve_storage_config(db)
    object_key = f"projects/{project_id}/recordings/{recording_id}/original.{extension}"
    try:
        storage.upload_fileobj(object_key, file.file, file.content_type or "application/octet-stream", storage_config)
    except RuntimeError as exc:
        return fail("STORAGE_CONFIG_MISSING", str(exc))
    except Exception as exc:
        return fail("STORAGE_UPLOAD_FAILED", f"文件写入 Bucket 失败：{exc}")

    recording = Recording(
        id=recording_id,
        project_id=project_id,
        file_name=file_name,
        object_key=object_key,
        storage_config_id="default",
        storage_provider=storage_config.get("provider", "railway_bucket"),
        storage_bucket_name=storage_config.get("bucket_name", ""),
        storage_endpoint=storage_config.get("endpoint", ""),
        storage_region=storage_config.get("region", "auto"),
        storage_path_prefix=storage_config.get("path_prefix", ""),
        mime_type=file.content_type or "application/octet-stream",
        extension=extension,
        file_size_bytes=size,
        duration_seconds=duration_seconds,
        status="queued",
        template_type=template_type or "customer_interview",
    )
    db.add(recording)
    job = create_job(db, recording.project_id, recording.id, "asr_transcription", metadata)
    db.commit()
    enqueue_job(job.id)
    return ok({"recording_id": recording.id, "object_key": object_key, "status": "queued", "job_id": job.id})


@app.get("/api/projects/{project_id}/recordings")
def list_recordings(project_id: str, request: Request, keyword: str = "", page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    query = db.query(Recording).filter_by(project_id=project_id)
    if keyword:
        query = query.filter(Recording.file_name.contains(keyword))
    total = query.count()
    items = query.order_by(Recording.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [recording_payload(item, db) for item in items], "page": page, "page_size": page_size, "total": total})


@app.get("/api/recordings/{recording_id}")
def get_recording(recording_id: str, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    return ok(recording_payload(recording, db))


@app.patch("/api/recordings/{recording_id}")
async def update_recording(recording_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    file_name = str(payload.get("file_name") or "").strip()
    if not file_name:
        return fail("VALIDATION_ERROR", "文件名称不能为空")
    recording.file_name = file_name
    project_file = db.query(ProjectFile).filter_by(recording_id=recording.id).first()
    if project_file:
        project_file.file_name = file_name
    db.commit()
    return ok(recording_payload(recording, db))


@app.delete("/api/recordings/{recording_id}")
def delete_recording(recording_id: str, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    storage.delete_prefix(f"projects/{recording.project_id}/recordings/{recording.id}/", storage.recording_config(recording))
    db.query(UsageRecord).filter_by(recording_id=recording.id).delete(synchronize_session=False)
    db.query(ExportFile).filter_by(recording_id=recording.id).delete(synchronize_session=False)
    db.delete(recording)
    db.commit()
    return ok({"deleted": True})


@app.post("/api/recordings/{recording_id}/play-url")
def play_url(recording_id: str, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    return ok({"url": storage.create_download_url(recording.object_key, storage_config=storage.recording_config(recording)), "expires_in_seconds": 3600})


@app.get("/api/recordings/{recording_id}/transcript")
def get_transcript(recording_id: str, request: Request, source: str = "clean", db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    if source == "raw":
        rows = db.query(RawTranscriptSegment).filter_by(recording_id=recording_id).order_by(RawTranscriptSegment.start_time_ms).all()
        return ok({"recording_id": recording_id, "source": "raw_asr", "segments": [segment_payload(r) for r in rows]})
    rows = db.query(CleanTranscriptSegment).filter_by(recording_id=recording_id).order_by(CleanTranscriptSegment.start_time_ms).all()
    raw_map = {r.id: r.text for r in db.query(RawTranscriptSegment).filter_by(recording_id=recording_id).all()}
    segments = [segment_payload(r) | {"raw_text": raw_map.get(r.raw_segment_id, ""), "edited": r.edited} for r in rows]
    return ok({"recording_id": recording_id, "source": "clean_ai", "segments": segments})


def segment_payload(seg):
    return {"segment_id": seg.id, "speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}


@app.patch("/api/transcript-segments/{segment_id}")
async def update_segment(segment_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    seg = db.get(CleanTranscriptSegment, segment_id)
    if not seg:
        return fail("SEGMENT_NOT_FOUND", "段落不存在", status_code=404)
    recording = db.get(Recording, seg.recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    old_speaker = seg.speaker
    new_speaker = payload.get("speaker", seg.speaker)
    updated_count = 1
    if payload.get("replace_same_speaker") and new_speaker != old_speaker:
        same_speaker_rows = db.query(CleanTranscriptSegment).filter_by(recording_id=seg.recording_id, speaker=old_speaker).all()
        for row in same_speaker_rows:
            row.speaker = new_speaker
            row.edited = True
        updated_count = len(same_speaker_rows)
    else:
        seg.speaker = new_speaker
        seg.edited = True
    if "text" in payload:
        seg.text = payload.get("text") or ""
        seg.edited = True
    recording.summary_stale = True
    summary = db.query(SummaryArtifact).filter_by(recording_id=recording.id).first()
    if summary:
        summary.stale = True
    db.commit()
    return ok({"segment_id": seg.id, "source": "clean_user_edited", "summary_stale": True, "updated_count": updated_count})


@app.get("/api/recordings/{recording_id}/summary")
def get_summary(recording_id: str, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if recording:
        user = current_user(request, db)
        try:
            ensure_project_access(recording.project_id, user, db)
        except HTTPException as exc:
            return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    summary = db.query(SummaryArtifact).filter_by(recording_id=recording_id).first()
    if not summary:
        return ok({"status": "empty", "content": None})
    return ok({"summary_id": summary.id, "recording_id": recording_id, "template_type": summary.template_type, "status": summary.status, "stale": summary.stale, "content": summary.content})


@app.post("/api/recordings/{recording_id}/summary/regenerate")
async def regenerate_summary(recording_id: str, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db, allow_shared=False)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    project_file = db.query(ProjectFile).filter_by(recording_id=recording.id).first()
    job = create_job(db, recording.project_id, recording.id, "summary_generation", {"file_id": project_file.id if project_file else None, "user_id": user.id})
    db.commit()
    enqueue_job(job.id)
    return ok({"job_id": job.id, "status": "queued"})


def _selected_file_ids_from_payload(payload: dict) -> list[str]:
    file_ids = [str(item) for item in (payload.get("file_ids") or []) if item]
    if file_ids:
        return file_ids[: settings.max_qa_recordings]
    recording_ids = [str(item) for item in (payload.get("recording_ids") or []) if item]
    return recording_ids[: settings.max_qa_recordings]


def _validate_qa_files(db: Session, project_id: str, selected_ids: list[str]) -> tuple[list[ProjectFile], list[str], list[str] | None]:
    if len(selected_ids) > settings.max_qa_recordings:
        return [], [], ["VALIDATION_ERROR", "最多选择 10 份文件"]
    if not selected_ids:
        return [], [], ["VALIDATION_ERROR", "请至少选择 1 份文件"]
    own_files = db.query(ProjectFile).filter(ProjectFile.project_id == project_id, ProjectFile.id.in_(selected_ids)).all()
    by_id = {item.id: item for item in own_files}
    selected_map: dict[str, ProjectFile] = {item.id: item for item in own_files}
    references = db.query(ProjectFileReference).filter(ProjectFileReference.target_project_id == project_id, ProjectFileReference.status == "active").all()
    for reference in references:
        if reference.source_file_id in selected_ids and reference.source_file_id not in by_id:
            source_file = db.get(ProjectFile, reference.source_file_id)
            if source_file:
                by_id[source_file.id] = source_file
                selected_map[reference.source_file_id] = source_file
    # Backward compatibility: callers may still submit recording IDs.
    recording_ids = [item for item in selected_ids if item not in selected_map]
    if recording_ids:
        rec_files = db.query(ProjectFile).filter(ProjectFile.project_id == project_id, ProjectFile.recording_id.in_(recording_ids)).all()
        for project_file in rec_files:
            by_id[project_file.id] = project_file
            selected_map[project_file.recording_id] = project_file
    files = [selected_map.get(item) for item in selected_ids if selected_map.get(item)]
    if len(files) != len(selected_ids):
        return [], [], ["VALIDATION_ERROR", "包含不属于当前项目或未引用的文件"]
    not_ready = [item.file_name for item in files if item.status != "completed"]
    if not_ready:
        return [], [], ["QA_FILE_NOT_READY", f"以下文件尚未处理完成，不能用于问答：{', '.join(not_ready[:3])}"]
    recording_ids_for_legacy = [item.recording_id for item in files if item.recording_id]
    return files, recording_ids_for_legacy, None



@app.post("/api/projects/{project_id}/qa-threads")
async def create_qa_thread(project_id: str, request: Request, payload: dict | None = None, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    thread = QAThread(id=new_id("qath"), project_id=project_id, user_id=user.id, title="新对话")
    db.add(thread)
    db.commit()
    return ok(thread_payload(thread, db))


@app.get("/api/projects/{project_id}/qa-threads")
def list_qa_threads(project_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    try:
        ensure_project_access(project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "项目不存在或无权访问", status_code=exc.status_code)
    query = db.query(QAThread).filter_by(project_id=project_id)
    if user.role != "admin":
        query = query.filter((QAThread.user_id == user.id) | (QAThread.user_id.is_(None)))
    rows = query.order_by(QAThread.updated_at.desc()).all()
    return ok({"items": [thread_payload(row, db) for row in rows]})


@app.get("/api/qa-threads/{thread_id}")
def get_qa_thread(thread_id: str, request: Request, db: Session = Depends(get_db)):
    thread = db.get(QAThread, thread_id)
    if not thread:
        return fail("QA_THREAD_NOT_FOUND", "对话不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(thread.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "对话不存在或无权访问", status_code=exc.status_code)
    if user.role != "admin" and thread.user_id not in {None, user.id}:
        return fail("QA_THREAD_FORBIDDEN", "无权访问该对话", status_code=403)
    return ok(thread_payload(thread, db, include_messages=True))


@app.post("/api/qa-threads/{thread_id}/messages")
async def create_qa_message(thread_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    thread = db.get(QAThread, thread_id)
    if not thread:
        return fail("QA_THREAD_NOT_FOUND", "对话不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(thread.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "对话不存在或无权访问", status_code=exc.status_code)
    pending = (
        db.query(QAMessage)
        .filter(QAMessage.thread_id == thread.id, QAMessage.role == "assistant", QAMessage.status.in_(["queued", "running"]))
        .first()
    )
    if pending:
        return fail("QA_IN_PROGRESS", "当前对话正在生成回答，请等待完成后再继续提问")
    selected_ids = _selected_file_ids_from_payload(payload)
    files, recording_ids, validation_error = _validate_qa_files(db, thread.project_id, selected_ids)
    if validation_error:
        return fail(validation_error[0], validation_error[1])
    question = (payload.get("question") or "").strip()
    if not question:
        return fail("VALIDATION_ERROR", "请输入问题")
    file_ids = [item.id for item in files]
    user_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, user_id=user.id, role="user", content=question, selected_recording_ids=recording_ids, selected_file_ids=file_ids, status="ready")
    assistant_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, user_id=user.id, role="assistant", content="", selected_recording_ids=recording_ids, selected_file_ids=file_ids, status="queued")
    db.add(user_msg)
    db.add(assistant_msg)
    if thread.title == "新对话":
        thread.title = question[:10]
    db.flush()
    job = create_job(db, thread.project_id, None, "qa_answer", {"thread_id": thread.id, "user_message_id": user_msg.id, "assistant_message_id": assistant_msg.id, "user_id": user.id})
    db.commit()
    enqueue_job(job.id)
    return ok({"thread_id": thread.id, "user_message_id": user_msg.id, "assistant_message_id": assistant_msg.id, "job_id": job.id, "status": "queued"})


@app.post("/api/qa-threads/{thread_id}/messages/stream")
async def create_qa_message_stream(thread_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    thread = db.get(QAThread, thread_id)
    if not thread:
        return fail("QA_THREAD_NOT_FOUND", "对话不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(thread.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "对话不存在或无权访问", status_code=exc.status_code)
    if user.role != "admin" and thread.user_id not in {None, user.id}:
        return fail("QA_THREAD_FORBIDDEN", "无权访问该对话", status_code=403)
    pending = (
        db.query(QAMessage)
        .filter(QAMessage.thread_id == thread.id, QAMessage.role == "assistant", QAMessage.status.in_(["queued", "running"]))
        .first()
    )
    if pending:
        return fail("QA_IN_PROGRESS", "当前对话正在生成回答，请等待完成后再继续提问")
    selected_ids = _selected_file_ids_from_payload(payload)
    files, recording_ids, validation_error = _validate_qa_files(db, thread.project_id, selected_ids)
    if validation_error:
        return fail(validation_error[0], validation_error[1])
    question = (payload.get("question") or "").strip()
    if not question:
        return fail("VALIDATION_ERROR", "请输入问题")

    file_ids = [item.id for item in files]
    user_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, user_id=user.id, role="user", content=question, selected_recording_ids=recording_ids, selected_file_ids=file_ids, status="ready")
    assistant_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, user_id=user.id, role="assistant", content="", reasoning_content="", selected_recording_ids=recording_ids, selected_file_ids=file_ids, status="running")
    db.add(user_msg)
    db.add(assistant_msg)
    if thread.title == "新对话":
        thread.title = question[:10]
    db.flush()
    materials, history, total_chars = _build_qa_context(db, thread, file_ids, user_msg.id, assistant_msg.id)
    if total_chars > CONTEXT_CHAR_LIMIT:
        db.rollback()
        return fail("LLM_CONTEXT_TOO_LONG", "已选文件内容超过模型上下文上限，请减少文件数量。")
    exceeded, reason = quota_exceeded(db, user, "qa", total_chars)
    if exceeded:
        db.rollback()
        return fail("USER_QUOTA_EXCEEDED", reason)
    job = create_job(db, thread.project_id, None, "qa_answer", {"thread_id": thread.id, "user_message_id": user_msg.id, "assistant_message_id": assistant_msg.id, "stream": True, "user_id": user.id})
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.progress = 20
    db.commit()

    return StreamingResponse(
        _stream_qa_answer(thread.id, user_msg.id, assistant_msg.id, job.id, question, materials, history, total_chars),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def thread_payload(thread: QAThread, db: Session, include_messages: bool = False):
    messages = db.query(QAMessage).filter_by(thread_id=thread.id).order_by(QAMessage.created_at.asc()).all()
    first_user = next((msg for msg in messages if msg.role == "user"), None)
    title = (first_user.content[:10] if first_user else thread.title) or "新对话"
    payload = {
        "thread_id": thread.id,
        "project_id": thread.project_id,
        "title": title,
        "created_at": serialize_dt(thread.created_at),
        "updated_at": serialize_dt(thread.updated_at),
        "last_message_at": serialize_dt(messages[-1].created_at if messages else thread.updated_at),
    }
    if include_messages:
        payload["messages"] = [message_payload(msg) for msg in messages]
    return payload


def message_payload(msg: QAMessage):
    return {
        "message_id": msg.id,
        "thread_id": msg.thread_id,
        "role": msg.role,
        "content": msg.content,
        "reasoning_content": msg.reasoning_content,
        "selected_recording_ids": msg.selected_recording_ids,
        "selected_file_ids": msg.selected_file_ids or msg.selected_recording_ids,
        "sources": msg.sources,
        "status": msg.status,
        "usage": msg.usage,
        "error_code": msg.error_code,
        "created_at": serialize_dt(msg.created_at),
    }


def _build_qa_context(db: Session, thread: QAThread, file_ids: list[str], user_message_id: str, assistant_message_id: str):
    project_files = db.query(ProjectFile).filter(ProjectFile.id.in_(file_ids)).all()
    file_by_id = {project_file.id: project_file for project_file in project_files}
    materials = []
    total_chars = 0
    for file_id in file_ids:
        project_file = file_by_id.get(file_id)
        if not project_file:
            continue
        if project_file.file_type == "audio" and project_file.recording_id:
            recording = db.get(Recording, project_file.recording_id)
            if not recording:
                continue
            segments = [
                {"speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}
                for seg in db.query(CleanTranscriptSegment).filter_by(recording_id=recording.id).order_by(CleanTranscriptSegment.start_time_ms).all()
            ]
            total_chars += sum(len(seg["text"]) for seg in segments)
            materials.append({"file_id": project_file.id, "recording_id": recording.id, "file_name": recording.file_name, "file_type": "audio", "segments": segments})
        else:
            text = project_file.extracted_text or ""
            total_chars += len(text)
            materials.append({"file_id": project_file.id, "file_name": project_file.file_name, "file_type": project_file.file_type, "text": text})
    previous_messages = (
        db.query(QAMessage)
        .filter(QAMessage.thread_id == thread.id, QAMessage.id.notin_([user_message_id, assistant_message_id]))
        .order_by(QAMessage.created_at.desc())
        .limit(8)
        .all()
    )
    history = [
        {
            "role": msg.role,
            "content": msg.content,
            "selected_recording_ids": msg.selected_recording_ids,
            "selected_file_ids": msg.selected_file_ids,
        }
        for msg in reversed(previous_messages)
    ]
    return materials, history, total_chars


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_qa_answer(thread_id: str, user_message_id: str, assistant_message_id: str, job_id: str, question: str, materials: list[dict], history: list[dict], total_chars: int):
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    yield _sse(
        "created",
        {
            "thread_id": thread_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "job_id": job_id,
        },
    )
    try:
        for chunk in llm_client.answer_stream(question, materials, history=history):
            chunk_type = chunk.get("type")
            delta = str(chunk.get("delta") or "")
            if not delta:
                continue
            if chunk_type == "reasoning":
                reasoning_parts.append(delta)
                yield _sse("reasoning", {"delta": delta})
            else:
                content_parts.append(delta)
                yield _sse("content", {"delta": delta})
        content = re.sub(r"[\s\(（\[]*seg_[0-9A-Za-z_-]+[\)）\]]*", "", "".join(content_parts)).strip()
        reasoning = "".join(reasoning_parts).strip()
        with session_scope() as session:
            assistant_message = session.get(QAMessage, assistant_message_id)
            thread = session.get(QAThread, thread_id)
            job = session.get(ProcessingJob, job_id)
            if assistant_message:
                assistant_message.content = content
                assistant_message.reasoning_content = reasoning
                assistant_message.status = "ready"
                assistant_message.usage = {"input_chars": total_chars, "model": get_ai_config("qa").get("model", get_settings().llm_qa_model), "stream": True}
            if thread:
                thread.updated_at = datetime.now(timezone.utc)
            if job:
                job.status = "succeeded"
                job.progress = 100
                job.finished_at = datetime.now(timezone.utc)
            session.add(
                UsageRecord(
                    id=new_id("use"),
                    project_id=thread.project_id if thread else None,
                    user_id=job.user_id if job else None,
                    job_id=job_id,
                    call_type="qa",
                    model_provider="aliyun/mock",
                    model_name=get_ai_config("qa").get("model", get_settings().llm_qa_model),
                    input_tokens=total_chars,
                    output_tokens=len(content),
                    status="succeeded",
                )
            )
        yield _sse(
            "done",
            {
                "content": content,
                "reasoning_content": reasoning,
                "assistant_message_id": assistant_message_id,
            },
        )
    except Exception as exc:
        with session_scope() as session:
            assistant_message = session.get(QAMessage, assistant_message_id)
            job = session.get(ProcessingJob, job_id)
            if assistant_message:
                assistant_message.content = "".join(content_parts).strip()
                assistant_message.reasoning_content = "".join(reasoning_parts).strip()
                assistant_message.status = "failed"
                assistant_message.error_code = "LLM_CALL_FAILED"
            if job:
                job.status = "failed"
                job.progress = 100
                job.error_code = "LLM_CALL_FAILED"
                job.error_message = str(exc)
                job.finished_at = datetime.now(timezone.utc)
        yield _sse("error", {"code": "LLM_CALL_FAILED", "message": str(exc)})


@app.get("/api/jobs/recent")
def recent_jobs(page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    rows = db.query(ProcessingJob).order_by(ProcessingJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [job_payload(j) for j in rows], "page": page, "page_size": page_size, "total": db.query(ProcessingJob).count()})


@app.get("/api/projects/{project_id}/jobs")
def project_jobs(project_id: str, status: str = "", page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    query = db.query(ProcessingJob).filter_by(project_id=project_id)
    if status:
        query = query.filter_by(status=status)
    total = query.count()
    rows = query.order_by(ProcessingJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [job_payload(j) for j in rows], "page": page, "page_size": page_size, "total": total})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProcessingJob, job_id)
    if not job:
        return fail("JOB_NOT_FOUND", "任务不存在", status_code=404)
    return ok(job_payload(job))


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    try:
        new_job_id = retry_failed_job(job_id)
    except ValueError:
        return fail("JOB_NOT_RETRYABLE", "只有失败任务可以重试")
    return ok({"new_job_id": new_job_id, "status": "queued"})


@app.post("/api/recordings/{recording_id}/exports")
async def create_export(recording_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    user = current_user(request, db)
    try:
        ensure_project_access(recording.project_id, user, db)
    except HTTPException as exc:
        return fail(str(exc.detail), "录音不存在或无权访问", status_code=exc.status_code)
    export_type = payload.get("export_type") or "summary"
    if export_type not in {"summary", "transcript"}:
        return fail("VALIDATION_ERROR", "只支持导出纪要或清洁稿 Markdown")
    export_id = new_id("exp")
    object_key = f"projects/{recording.project_id}/exports/{export_id}/{export_type}.md"
    content = _build_markdown(db, recording, export_type)
    storage.save_text(object_key, content)
    export = ExportFile(id=export_id, project_id=recording.project_id, recording_id=recording.id, export_type=export_type, format="markdown", object_key=object_key, status="ready")
    db.add(export)
    db.commit()
    return ok({"export_id": export.id, "status": "ready", "filename": _export_filename(recording.file_name, export_type), "content": content, "download_url": storage.create_download_url(object_key)})


@app.get("/api/exports/{export_id}")
def get_export(export_id: str, db: Session = Depends(get_db)):
    export = db.get(ExportFile, export_id)
    if not export:
        return fail("EXPORT_NOT_FOUND", "导出不存在", status_code=404)
    return ok({"export_id": export.id, "status": export.status, "download_url": storage.create_download_url(export.object_key), "expires_in_seconds": 3600})


def _build_markdown(db: Session, recording: Recording, export_type: str) -> str:
    if export_type == "transcript":
        rows = db.query(CleanTranscriptSegment).filter_by(recording_id=recording.id).order_by(CleanTranscriptSegment.start_time_ms).all()
        lines = [f"# {recording.file_name} 清洁稿", ""]
        lines += [f"[{fmt_time(seg.start_time_ms)}] **{seg.speaker}**：{seg.text}" for seg in rows]
        return "\n\n".join(lines)
    summary = db.query(SummaryArtifact).filter_by(recording_id=recording.id).first()
    content = summary.content if summary else {}
    markdown = content.get("markdown") if isinstance(content, dict) else ""
    return markdown or f"# {recording.file_name} 访谈纪要\n\n暂无纪要内容。"


def fmt_time(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _export_filename(file_name: str, export_type: str) -> str:
    stem = Path(file_name).stem or "recording"
    safe_stem = re.sub(r'[\\/:*?"<>|]+', "_", stem).strip() or "recording"
    suffix = "清洁稿" if export_type == "transcript" else "纪要"
    return f"{safe_stem}-{suffix}.md"


@app.get("/api/settings")
def get_app_settings_api(db: Session = Depends(get_db)):
    return ok(public_app_settings(db))


@app.patch("/api/settings")
async def patch_settings(payload: dict, db: Session = Depends(get_db)):
    save_app_settings(db, payload)
    db.commit()
    data = public_app_settings(db)
    data["saved"] = True
    return ok(data)


@app.post("/api/settings/storage/test")
async def test_storage_settings(payload: dict, db: Session = Depends(get_db)):
    config = resolve_storage_config(db, payload)
    try:
        result = storage.test_connection(config)
    except ValueError as exc:
        return fail("STORAGE_CONFIG_INVALID", str(exc))
    except Exception as exc:
        return fail("STORAGE_TEST_FAILED", f"存储连接失败：{exc}")
    return ok({
        "status": result.get("status", "passed"),
        "message": result.get("message", "存储连接成功"),
        "provider": config.get("provider", "local"),
        "bucket_name": config.get("bucket_name", ""),
        "endpoint": config.get("endpoint", ""),
    })


@app.post("/api/settings/ai/{node}/test")
async def test_ai_settings(node: str, payload: dict, db: Session = Depends(get_db)):
    if node not in AI_NODES:
        return fail("VALIDATION_ERROR", "不支持的 AI 节点")
    current = get_app_settings(db)["ai"][node]
    model = str(payload.get("model") or current.get("model") or "").strip()
    url = str(payload.get("url") or current.get("url") or "").strip()
    key_value = payload.get("api_key", payload.get("key"))
    api_key = str(key_value or "").strip() or current.get("api_key", "")
    if not model:
        return fail("AI_MODEL_REQUIRED", "请先填写模型名称")
    if not url.startswith(("http://", "https://")):
        return fail("AI_URL_INVALID", "请填写有效的 API URL")
    if not api_key:
        return fail("AI_KEY_REQUIRED", "请先填写或保存 API Key")

    started = time.perf_counter()
    try:
        if node == "asr":
            message = _test_asr_config(url, api_key, model)
        else:
            message = _test_llm_config(url, api_key, model)
    except requests.Timeout:
        return fail("AI_TEST_TIMEOUT", "测试连接超时，请检查 URL 或网络")
    except requests.RequestException as exc:
        return fail("AI_TEST_FAILED", f"测试连接失败：{exc}")
    except ValueError as exc:
        return fail("AI_TEST_FAILED", str(exc))

    return ok({
        "node": node,
        "status": "passed",
        "message": message,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "model": model,
        "url": url,
    })


def _test_llm_config(base_url: str, api_key: str, model: str) -> str:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "请只回复 ok，用于测试模型连接。"}],
            "temperature": 0,
            "max_tokens": 8,
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise ValueError(f"模型接口返回 {response.status_code}：{_safe_response_text(response)}")
    data = response.json()
    if not data.get("choices"):
        raise ValueError("模型接口返回成功，但没有 choices 结果")
    return "模型连接成功，已收到测试响应"


def _test_asr_config(url: str, api_key: str, model: str) -> str:
    response = requests.post(
        url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
        json={"model": model, "input": {}, "parameters": {"language_hints": ["zh", "en"]}},
        timeout=20,
    )
    if response.status_code < 400:
        return "ASR 接口连接成功"
    body = _safe_response_text(response)
    lowered = body.lower()
    auth_or_model_error = any(
        marker in lowered
        for marker in ["unauthorized", "forbidden", "invalid api", "api-key", "apikey", "access denied", "model", "not found"]
    ) or any(marker in body for marker in ["鉴权", "认证", "权限", "模型", "不存在"])
    if response.status_code in {400, 422} and not auth_or_model_error:
        return "ASR 接口可达且鉴权未被拒绝；未提交音频，仅完成配置连通性测试"
    raise ValueError(f"ASR 接口返回 {response.status_code}：{body}")


def _safe_response_text(response: requests.Response) -> str:
    try:
        data = response.json()
        message = data.get("message") or data.get("error", {}).get("message") or data.get("code") or str(data)
    except ValueError:
        message = response.text
    return str(message).replace("\n", " ")[:500]


@app.get("/api/usage/overview")
def usage_overview(db: Session = Depends(get_db)):
    rows = db.query(UsageRecord).all()
    return ok({"total_audio_duration_seconds": sum(r.audio_duration_seconds for r in rows), "total_asr_duration_seconds": sum(r.audio_duration_seconds for r in rows if r.call_type == "asr"), "total_input_tokens": sum(r.input_tokens for r in rows), "total_output_tokens": sum(r.output_tokens for r in rows), "estimated_cost": sum(r.cost_estimate for r in rows)})


@app.get("/api/usage/projects")
def usage_projects(db: Session = Depends(get_db)):
    items = []
    for project in db.query(Project).all():
        recs = db.query(Recording).filter_by(project_id=project.id).all()
        usage = db.query(UsageRecord).filter_by(project_id=project.id).all()
        items.append({"project_id": project.id, "project_name": project.title, "recording_count": len(recs), "audio_duration_seconds": sum(r.duration_seconds for r in recs), "llm_tokens": sum(u.input_tokens + u.output_tokens for u in usage), "estimated_cost": sum(u.cost_estimate for u in usage)})
    return ok({"items": items})


@app.get("/api/usage/jobs")
def usage_jobs(project_id: str = "", db: Session = Depends(get_db)):
    query = db.query(UsageRecord)
    if project_id:
        query = query.filter_by(project_id=project_id)
    rows = query.order_by(UsageRecord.created_at.desc()).limit(100).all()
    return ok({"items": [{"usage_id": r.id, "project_id": r.project_id, "recording_id": r.recording_id, "job_id": r.job_id, "call_type": r.call_type, "model_name": r.model_name, "audio_duration_seconds": r.audio_duration_seconds, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "estimated_cost": r.cost_estimate, "created_at": serialize_dt(r.created_at)} for r in rows]})


@app.get("/api/admin/usage/projects")
def admin_usage_projects(request: Request, project_id: str = "", db: Session = Depends(get_db)):
    require_admin_user(request, db)
    query = db.query(Project)
    if project_id:
        query = query.filter_by(id=project_id)
    items = []
    for project in query.order_by(Project.updated_at.desc()).all():
        files = db.query(ProjectFile).filter_by(project_id=project.id).all()
        usage = db.query(UsageRecord).filter_by(project_id=project.id).all()
        qa_usage = [row for row in usage if row.call_type == "qa"]
        items.append(
            {
                "project_id": project.id,
                "project_name": project.title,
                "file_count": len(files),
                "audio_duration_seconds": sum(item.duration_seconds or 0 for item in files if item.file_type == "audio"),
                "qa_count": len(qa_usage),
                "qa_input_tokens": sum(row.input_tokens or 0 for row in qa_usage),
                "qa_output_tokens": sum(row.output_tokens or 0 for row in qa_usage),
            }
        )
    return ok({"items": items})


@app.get("/api/admin/usage/users")
def admin_usage_users(request: Request, user_id: str = "", db: Session = Depends(get_db)):
    require_admin_user(request, db)
    query = db.query(User)
    if user_id:
        query = query.filter_by(id=user_id)
    items = []
    for user in query.order_by(User.created_at.desc()).all():
        usage = db.query(UsageRecord).filter_by(user_id=user.id).all()
        qa_usage = [row for row in usage if row.call_type == "qa"]
        asr_usage = [row for row in usage if row.call_type == "asr"]
        items.append(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name or user.username,
                "audio_duration_seconds": sum(row.audio_duration_seconds or 0 for row in asr_usage),
                "asr_count": len(asr_usage),
                "qa_count": len(qa_usage),
                "qa_input_tokens": sum(row.input_tokens or 0 for row in qa_usage),
                "qa_output_tokens": sum(row.output_tokens or 0 for row in qa_usage),
            }
        )
    return ok({"items": items})


@app.get("/api/diagnostics/errors")
def diagnostics_errors(db: Session = Depends(get_db)):
    rows = db.query(ProcessingJob).filter_by(status="failed").order_by(ProcessingJob.updated_at.desc()).limit(50).all()
    return ok({"items": [job_payload(row) for row in rows]})


@app.post("/api/diagnostics/export")
async def diagnostics_export(payload: dict):
    return ok({"export_id": new_id("expdiag"), "status": "ready", "message": "MVP 诊断包接口占位，后续补充 zip 导出"})


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        return FileResponse(frontend_dist / "index.html")
