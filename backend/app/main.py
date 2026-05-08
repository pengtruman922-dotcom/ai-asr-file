from pathlib import Path
import time

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, init_db
from .models import (
    CleanTranscriptSegment,
    ExportFile,
    ProcessingJob,
    QAMessage,
    Project,
    QASession,
    QAThread,
    RawTranscriptSegment,
    Recording,
    SummaryArtifact,
    UsageRecord,
)
from .storage import storage
from .settings_service import AI_NODES, get_app_settings, get_basic_config, public_app_settings, resolve_storage_config, save_app_settings
from .tasks import create_job, enqueue_job, retry_failed_job
from .utils import fail, make_token, new_id, ok, require_auth, serialize_dt

settings = get_settings()
app = FastAPI(title="AI ASR File MVP")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}


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


def project_payload(project: Project, db: Session):
    recs = db.query(Recording).filter_by(project_id=project.id).all()
    return {
        "project_id": project.id,
        "title": project.title,
        "description": project.description,
        "recording_count": len(recs),
        "total_duration_seconds": sum(r.duration_seconds or 0 for r in recs),
        "created_at": serialize_dt(project.created_at),
        "updated_at": serialize_dt(project.updated_at),
    }


def recording_payload(recording: Recording):
    return {
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


def job_payload(job: ProcessingJob):
    return {
        "job_id": job.id,
        "project_id": job.project_id,
        "recording_id": job.recording_id,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
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
async def login(payload: dict):
    if payload.get("username") == settings.admin_username and payload.get("password") == settings.admin_password:
        return ok({"token": make_token(settings.admin_username), "username": settings.admin_username})
    return fail("UNAUTHORIZED", "账号或密码错误", status_code=401)


@app.get("/api/auth/me")
def me(request: Request):
    return ok({"username": require_auth(request)})


@app.post("/api/auth/logout")
def logout():
    return ok({})


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
async def create_project(payload: dict, db: Session = Depends(get_db)):
    title = (payload.get("title") or "").strip()
    if not title:
        return fail("VALIDATION_ERROR", "项目名称不能为空")
    project = Project(id=new_id("proj"), title=title, description=payload.get("description", "") or "")
    db.add(project)
    db.commit()
    db.refresh(project)
    return ok(project_payload(project, db))


@app.get("/api/projects")
def list_projects(keyword: str = "", page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    query = db.query(Project)
    if keyword:
        query = query.filter(Project.title.contains(keyword))
    total = query.count()
    items = query.order_by(Project.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [project_payload(item, db) for item in items], "page": page, "page_size": page_size, "total": total})


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    payload = project_payload(project, db)
    payload["stats"] = dict(db.query(Recording.status, func.count(Recording.id)).filter_by(project_id=project_id).group_by(Recording.status).all())
    return ok(payload)


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, payload: dict, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    project.title = payload.get("title", project.title)
    project.description = payload.get("description", project.description)
    db.commit()
    return ok(project_payload(project, db))


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    for recording in db.query(Recording).filter_by(project_id=project_id).all():
        storage.delete_prefix(f"projects/{project_id}/recordings/{recording.id}/", storage.recording_config(recording))
    storage.delete_prefix(f"projects/{project_id}/")
    db.query(UsageRecord).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QASession).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QAMessage).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(QAThread).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ExportFile).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ProcessingJob).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.delete(project)
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
    job = create_job(db, recording.project_id, recording.id, "asr_transcription")
    db.commit()
    enqueue_job(job.id)
    return ok({"recording_id": recording.id, "status": "queued", "job_id": job.id})


@app.post("/api/projects/{project_id}/recordings/upload")
def upload_recording_proxy(
    project_id: str,
    file: UploadFile = File(...),
    duration_seconds: int = Form(0),
    template_type: str = Form("customer_interview"),
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
    job = create_job(db, recording.project_id, recording.id, "asr_transcription")
    db.commit()
    enqueue_job(job.id)
    return ok({"recording_id": recording.id, "object_key": object_key, "status": "queued", "job_id": job.id})


@app.get("/api/projects/{project_id}/recordings")
def list_recordings(project_id: str, keyword: str = "", page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    query = db.query(Recording).filter_by(project_id=project_id)
    if keyword:
        query = query.filter(Recording.file_name.contains(keyword))
    total = query.count()
    items = query.order_by(Recording.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [recording_payload(item) for item in items], "page": page, "page_size": page_size, "total": total})


@app.get("/api/recordings/{recording_id}")
def get_recording(recording_id: str, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    return ok(recording_payload(recording))


@app.delete("/api/recordings/{recording_id}")
def delete_recording(recording_id: str, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    storage.delete_prefix(f"projects/{recording.project_id}/recordings/{recording.id}/", storage.recording_config(recording))
    db.query(UsageRecord).filter_by(recording_id=recording.id).delete(synchronize_session=False)
    db.query(ExportFile).filter_by(recording_id=recording.id).delete(synchronize_session=False)
    db.delete(recording)
    db.commit()
    return ok({"deleted": True})


@app.post("/api/recordings/{recording_id}/play-url")
def play_url(recording_id: str, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    return ok({"url": storage.create_download_url(recording.object_key, storage_config=storage.recording_config(recording)), "expires_in_seconds": 3600})


@app.get("/api/recordings/{recording_id}/transcript")
def get_transcript(recording_id: str, source: str = "clean", db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
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
async def update_segment(segment_id: str, payload: dict, db: Session = Depends(get_db)):
    seg = db.get(CleanTranscriptSegment, segment_id)
    if not seg:
        return fail("SEGMENT_NOT_FOUND", "段落不存在", status_code=404)
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
    recording = db.get(Recording, seg.recording_id)
    recording.summary_stale = True
    summary = db.query(SummaryArtifact).filter_by(recording_id=recording.id).first()
    if summary:
        summary.stale = True
    db.commit()
    return ok({"segment_id": seg.id, "source": "clean_user_edited", "summary_stale": True, "updated_count": updated_count})


@app.get("/api/recordings/{recording_id}/summary")
def get_summary(recording_id: str, db: Session = Depends(get_db)):
    summary = db.query(SummaryArtifact).filter_by(recording_id=recording_id).first()
    if not summary:
        return ok({"status": "empty", "content": None})
    return ok({"summary_id": summary.id, "recording_id": recording_id, "template_type": summary.template_type, "status": summary.status, "stale": summary.stale, "content": summary.content})


@app.post("/api/recordings/{recording_id}/summary/regenerate")
async def regenerate_summary(recording_id: str, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    job = create_job(db, recording.project_id, recording.id, "summary_generation")
    db.commit()
    enqueue_job(job.id)
    return ok({"job_id": job.id, "status": "queued"})



@app.post("/api/projects/{project_id}/qa-threads")
async def create_qa_thread(project_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        return fail("PROJECT_NOT_FOUND", "项目不存在", status_code=404)
    thread = QAThread(id=new_id("qath"), project_id=project_id, title="新对话")
    db.add(thread)
    db.commit()
    return ok(thread_payload(thread, db))


@app.get("/api/projects/{project_id}/qa-threads")
def list_qa_threads(project_id: str, db: Session = Depends(get_db)):
    rows = db.query(QAThread).filter_by(project_id=project_id).order_by(QAThread.updated_at.desc()).all()
    return ok({"items": [thread_payload(row, db) for row in rows]})


@app.get("/api/qa-threads/{thread_id}")
def get_qa_thread(thread_id: str, db: Session = Depends(get_db)):
    thread = db.get(QAThread, thread_id)
    if not thread:
        return fail("QA_THREAD_NOT_FOUND", "对话不存在", status_code=404)
    return ok(thread_payload(thread, db, include_messages=True))


@app.post("/api/qa-threads/{thread_id}/messages")
async def create_qa_message(thread_id: str, payload: dict, db: Session = Depends(get_db)):
    thread = db.get(QAThread, thread_id)
    if not thread:
        return fail("QA_THREAD_NOT_FOUND", "对话不存在", status_code=404)
    recording_ids = payload.get("recording_ids") or []
    if len(recording_ids) > settings.max_qa_recordings:
        return fail("VALIDATION_ERROR", "最多选择 10 份录音")
    if not recording_ids:
        return fail("VALIDATION_ERROR", "请至少选择 1 份录音")
    question = (payload.get("question") or "").strip()
    if not question:
        return fail("VALIDATION_ERROR", "请输入问题")
    valid_count = db.query(Recording).filter(Recording.project_id == thread.project_id, Recording.id.in_(recording_ids)).count()
    if valid_count != len(recording_ids):
        return fail("VALIDATION_ERROR", "包含不属于当前项目的录音")
    user_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, role="user", content=question, selected_recording_ids=recording_ids, status="ready")
    assistant_msg = QAMessage(id=new_id("qamsg"), thread_id=thread.id, project_id=thread.project_id, role="assistant", content="", selected_recording_ids=recording_ids, status="queued")
    db.add(user_msg)
    db.add(assistant_msg)
    if thread.title == "新对话":
        thread.title = question[:10]
    db.flush()
    job = create_job(db, thread.project_id, None, "qa_answer", {"thread_id": thread.id, "user_message_id": user_msg.id, "assistant_message_id": assistant_msg.id})
    db.commit()
    enqueue_job(job.id)
    return ok({"thread_id": thread.id, "user_message_id": user_msg.id, "assistant_message_id": assistant_msg.id, "job_id": job.id, "status": "queued"})


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
        "selected_recording_ids": msg.selected_recording_ids,
        "sources": msg.sources,
        "status": msg.status,
        "usage": msg.usage,
        "error_code": msg.error_code,
        "created_at": serialize_dt(msg.created_at),
    }


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
async def create_export(recording_id: str, payload: dict, db: Session = Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if not recording:
        return fail("RECORDING_NOT_FOUND", "录音不存在", status_code=404)
    export_type = payload.get("export_type") or "summary"
    if export_type not in {"summary", "transcript"}:
        return fail("VALIDATION_ERROR", "只支持导出纪要或清洁稿 Markdown")
    export_id = new_id("exp")
    object_key = f"projects/{recording.project_id}/exports/{export_id}/{export_type}.md"
    storage.save_text(object_key, _build_markdown(db, recording, export_type))
    export = ExportFile(id=export_id, project_id=recording.project_id, recording_id=recording.id, export_type=export_type, format="markdown", object_key=object_key, status="ready")
    db.add(export)
    db.commit()
    return ok({"export_id": export.id, "status": "ready", "download_url": storage.create_download_url(object_key)})


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
