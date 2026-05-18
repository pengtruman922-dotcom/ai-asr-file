from datetime import datetime, timedelta, timezone
from threading import Lock

from sqlalchemy import delete

from .clients import asr_client, llm_client
from .config import get_settings
from .database import session_scope
from .extractors import extract_content
from .settings_service import get_ai_config
from .models import (
    CleanTranscriptSegment,
    ProcessingJob,
    Project,
    ProjectFile,
    QAMessage,
    QASession,
    QAThread,
    RawTranscriptSegment,
    Recording,
    SummaryArtifact,
    UsageRecord,
)
from .storage import storage
from .utils import new_id


JOB_QUEUE_MAP = {
    "asr_transcription": "asr",
    "clean_transcript": "llm",
    "summary_generation": "llm",
    "qa_answer": "llm",
    "extract_text": "extract",
    "export": "default",
}
FINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}


def queue_name_for_job_type(job_type: str) -> str:
    settings = get_settings()
    if settings.task_queue_routing.strip().lower() == "split":
        return JOB_QUEUE_MAP.get(job_type, "default")
    return "default"


def enqueue_job(job_id: str, delay_seconds: int = 0) -> None:
    settings = get_settings()
    if settings.app_env == "local" and settings.queue_sync:
        run_job(job_id)
        return
    from redis import Redis
    from rq import Queue

    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        metadata = dict(job.metadata_json or {}) if job else {}
        queue_name = str(metadata.get("queue_name") or (queue_name_for_job_type(job.job_type) if job else "default"))
        if job and metadata.get("queue_name") != queue_name:
            metadata["queue_name"] = queue_name
            job.metadata_json = metadata

    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))
    if delay_seconds > 0:
        queue.enqueue_in(timedelta(seconds=delay_seconds), "app.tasks.run_job", job_id, job_timeout="6h")
    else:
        queue.enqueue("app.tasks.run_job", job_id, job_timeout="6h")


def create_job(session, project_id: str | None, recording_id: str | None, job_type: str, metadata: dict | None = None) -> ProcessingJob:
    metadata = dict(metadata or {})
    if not metadata.get("user_id"):
        inferred_user_id = None
        if metadata.get("file_id"):
            project_file = session.get(ProjectFile, metadata.get("file_id"))
            inferred_user_id = project_file.created_by_id if project_file else None
        if not inferred_user_id and project_id:
            project = session.get(Project, project_id)
            inferred_user_id = project.owner_id if project else None
        if inferred_user_id:
            metadata["user_id"] = inferred_user_id
    metadata.setdefault("queue_name", queue_name_for_job_type(job_type))
    job = ProcessingJob(
        id=new_id("job"),
        project_id=project_id,
        recording_id=recording_id,
        file_id=metadata.get("file_id"),
        user_id=metadata.get("user_id"),
        job_type=job_type,
        status="queued",
        metadata_json=metadata,
    )
    session.add(job)
    session.flush()
    return job


def run_job(job_id: str) -> None:
    job_type = ""
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job:
            return
        if job.status in FINAL_JOB_STATUSES:
            return
        job_type = job.job_type
        job.status = "running"
        if not job.started_at:
            job.started_at = datetime.now(timezone.utc)
        job.progress = max(job.progress or 0, 10)
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        project_file = session.get(ProjectFile, job.file_id) if job.file_id else None
        if recording and job.job_type == "clean_transcript":
            recording.status = "cleaning"
            if project_file:
                project_file.status = "cleaning"
        elif recording and job.job_type == "summary_generation":
            recording.status = "summary_generating"
            if project_file:
                project_file.status = "summary_generating"
        elif job.job_type == "extract_text" and job.file_id:
            project_file = session.get(ProjectFile, job.file_id)
            if project_file:
                project_file.status = "extracting"
                project_file.extraction_status = "running"
        elif job.job_type == "qa_answer":
            message_id = (job.metadata_json or {}).get("assistant_message_id")
            if message_id:
                message = session.get(QAMessage, message_id)
                if message:
                    message.status = "running"

    try:
        if job_type == "asr_transcription":
            _run_asr(job_id)
        elif job_type == "clean_transcript":
            _run_clean(job_id)
        elif job_type == "summary_generation":
            _run_summary(job_id)
        elif job_type == "qa_answer":
            _run_qa(job_id)
        elif job_type == "extract_text":
            _run_extract_text(job_id)
        elif job_type == "export":
            _mark_succeeded(job_id)
        else:
            raise RuntimeError(f"Unsupported job type: {job_type}")
    except Exception as exc:
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if job:
                message = str(exc)
                job.status = "failed"
                job.error_code = _error_code_for(job_type)
                job.error_message = message
                job.finished_at = datetime.now(timezone.utc)
                job.progress = 100
                if job_type == "asr_transcription":
                    _update_asr_failure_metadata(job, message)
                if job.recording_id:
                    recording = session.get(Recording, job.recording_id)
                    if recording:
                        recording.status = "failed"
                if job.file_id:
                    project_file = session.get(ProjectFile, job.file_id)
                    if project_file:
                        project_file.status = "failed"
                        project_file.extraction_status = "failed" if job_type == "extract_text" else project_file.extraction_status
            message_id = (job.metadata_json or {}).get("assistant_message_id") if job else None
            if message_id:
                message = session.get(QAMessage, message_id)
                if message:
                    message.status = "failed"
                    message.error_code = "LLM_CONTEXT_TOO_LONG" if "CONTEXT_TOO_LONG" in str(exc) else "LLM_CALL_FAILED"


def _retry_metadata(job_type: str, metadata: dict | None) -> dict:
    copied = dict(metadata or {})
    if job_type != "asr_transcription":
        return copied
    keep_asr_keys = {"asr_speaker_count", "asr_speaker_mode"}
    for key in list(copied.keys()):
        if key.startswith("asr_") and key not in keep_asr_keys:
            copied.pop(key, None)
    return copied


def retry_failed_job(job_id: str) -> str:
    with session_scope() as session:
        old = session.get(ProcessingJob, job_id)
        if not old or old.status != "failed":
            raise ValueError("JOB_NOT_RETRYABLE")
        new = create_job(session, old.project_id, old.recording_id, old.job_type, _retry_metadata(old.job_type, old.metadata_json))
        new.file_id = old.file_id
        new.user_id = old.user_id
        new_id_value = new.id
        if old.recording_id:
            recording = session.get(Recording, old.recording_id)
            if recording:
                if old.job_type == "asr_transcription":
                    recording.status = "queued"
                elif old.job_type == "clean_transcript":
                    recording.status = "asr_completed"
                elif old.job_type == "summary_generation":
                    recording.status = "cleaning_completed"
        if old.file_id:
            project_file = session.get(ProjectFile, old.file_id)
            if project_file:
                if old.job_type == "extract_text":
                    project_file.status = "queued"
                    project_file.extraction_status = "queued"
                elif old.job_type == "asr_transcription":
                    project_file.status = "queued"
                elif old.job_type == "clean_transcript":
                    project_file.status = "asr_completed"
                elif old.job_type == "summary_generation":
                    project_file.status = "cleaning_completed"
    enqueue_job(new_id_value)
    return new_id_value


def cancel_queued_job(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job:
            raise ValueError("JOB_NOT_FOUND")
        if job.status != "queued":
            raise ValueError("JOB_NOT_CANCELABLE")

        job.status = "canceled"
        job.progress = 100
        job.error_code = "JOB_CANCELED"
        job.error_message = "用户取消排队任务"
        job.finished_at = datetime.now(timezone.utc)
        metadata = dict(job.metadata_json or {})
        metadata["canceled_at"] = job.finished_at.isoformat()
        job.metadata_json = metadata

        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        project_file = session.get(ProjectFile, job.file_id) if job.file_id else None
        if job.job_type == "asr_transcription":
            if recording:
                recording.status = "canceled"
            if project_file:
                project_file.status = "canceled"
        elif job.job_type == "extract_text" and project_file:
            project_file.status = "canceled"
            project_file.extraction_status = "canceled"

        message_id = metadata.get("assistant_message_id")
        if message_id:
            message = session.get(QAMessage, message_id)
            if message:
                message.status = "canceled"
                message.error_code = "JOB_CANCELED"


def _asr_speaker_count_from_metadata(metadata: dict | None) -> int | None:
    if not metadata or "asr_speaker_count" not in metadata:
        return None
    try:
        value = int(metadata.get("asr_speaker_count") or 0)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 2, 3, 4} else None



class _NoopLock:
    def release(self) -> None:
        return None


def _acquire_redis_lock(lock_name: str, timeout: int = 60, blocking_timeout: int = 1):
    settings = get_settings()
    if settings.app_env == "local" and settings.queue_sync:
        return _NoopLock()
    from redis import Redis

    lock = Redis.from_url(settings.redis_url).lock(lock_name, timeout=timeout)
    if not lock.acquire(blocking=True, blocking_timeout=blocking_timeout):
        return None
    return lock


def _release_lock(lock) -> None:
    try:
        lock.release()
    except Exception:
        pass


def _asr_delay_seconds(value: int | None = None) -> int:
    settings = get_settings()
    raw_value = settings.asr_queue_retry_seconds if value is None else value
    try:
        return max(1, int(raw_value or 1))
    except (TypeError, ValueError):
        return 30


def _is_asr_throttled_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "http 429" in message or "throttl" in message or "rate limit" in message or "too many requests" in message


def _asr_failure_hint(message: str) -> str:
    lower = (message or "").lower()
    if "content_length_check_failed" in lower:
        return "DashScope 下载音频时 Content-Length 校验失败，优先检查 Bucket 下载 URL、文件完整性和对象大小。"
    if "download" in lower or "invalidfile.downloadfailed" in lower:
        return "DashScope 无法下载音频文件，优先检查 Bucket 预签名 URL 是否可公网完整下载。"
    if "unknown error" in lower:
        return "DashScope 返回未知处理错误，可先重试；若稳定复现，请检查音频编码、文件损坏或声道/说话人分离参数。"
    if "diarization" in lower or "speaker" in lower or "channel" in lower:
        return "错误可能与说话人分离或声道参数有关，可尝试转为单声道或关闭说话人分离后重试。"
    if "unsupported" in lower or "format" in lower or "decode" in lower:
        return "错误可能与音频格式或编码有关，建议转换为标准 mp3/wav/m4a(AAC) 后重试。"
    return ""


def _update_asr_failure_metadata(job, message: str) -> None:
    metadata = dict(job.metadata_json or {})
    diagnostics = dict(metadata.get("asr_diagnostics") or {})
    diagnostics["failure_hint"] = _asr_failure_hint(message)
    diagnostics["failure_message"] = message[:1000]
    metadata["asr_diagnostics"] = diagnostics
    job.metadata_json = metadata


def _parse_utc_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _asr_project_file(session, job: ProcessingJob, recording: Recording | None = None) -> ProjectFile | None:
    if job.file_id:
        project_file = session.get(ProjectFile, job.file_id)
        if project_file:
            return project_file
    if recording:
        return session.query(ProjectFile).filter_by(recording_id=recording.id).first()
    return None


def _asr_active_counts(session, user_id: str | None, exclude_job_id: str) -> dict:
    rows = session.query(ProcessingJob).filter(ProcessingJob.job_type == "asr_transcription", ProcessingJob.status == "running").all()
    global_active = 0
    user_active = 0
    for row in rows:
        if row.id == exclude_job_id:
            continue
        metadata = dict(row.metadata_json or {})
        phase = str(metadata.get("asr_phase") or "")
        has_external_task = bool(row.external_task_id or metadata.get("asr_task_id"))
        if not has_external_task and phase not in {"submitted", "polling", "downloading", "finalizing"}:
            continue
        global_active += 1
        if user_id and row.user_id == user_id:
            user_active += 1
    return {"global_active": global_active, "user_active": user_active}


def _asr_capacity_wait(counts: dict, user_id: str | None) -> tuple[str, dict] | None:
    settings = get_settings()
    user_limit = int(settings.asr_user_concurrency_limit or 0)
    global_limit = int(settings.asr_global_concurrency_limit or 0)
    detail = {
        "user_active": counts.get("user_active", 0),
        "user_limit": user_limit,
        "global_active": counts.get("global_active", 0),
        "global_limit": global_limit,
    }
    if user_id and user_limit > 0 and detail["user_active"] >= user_limit:
        return "user_limit", detail
    if global_limit > 0 and detail["global_active"] >= global_limit:
        return "global_limit", detail
    return None


def _mark_asr_waiting(job_id: str, reason: str, detail: dict | None = None, delay_seconds: int | None = None) -> None:
    delay = _asr_delay_seconds(delay_seconds)
    now = datetime.now(timezone.utc)
    next_retry_at = now + timedelta(seconds=delay)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        metadata = dict(job.metadata_json or {})
        metadata.update(
            {
                "asr_phase": "waiting_capacity",
                "asr_queue_reason": reason,
                "asr_capacity": detail or {},
                "asr_next_retry_at": next_retry_at.isoformat(),
            }
        )
        job.metadata_json = metadata
        job.status = "queued"
        job.started_at = None
        job.progress = 5
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        project_file = _asr_project_file(session, job, recording)
        if recording and not job.external_task_id:
            recording.status = "queued"
        if project_file and not job.external_task_id:
            project_file.status = "queued"
    enqueue_job(job_id, delay_seconds=delay)


def _reschedule_locked_asr(job_id: str) -> None:
    delay = _asr_delay_seconds(5)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        metadata = dict(job.metadata_json or {})
        has_external_task = bool(job.external_task_id or metadata.get("asr_task_id"))
    if has_external_task:
        enqueue_job(job_id, delay_seconds=delay)
    else:
        _mark_asr_waiting(job_id, "job_lock_busy", delay_seconds=delay)


def _prepare_asr_submission(job_id: str) -> tuple[str, str, int | None, str]:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job:
            raise RuntimeError("JOB_NOT_FOUND")
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        if not recording:
            raise RuntimeError("RECORDING_NOT_FOUND")
        audio_url = storage.create_download_url(recording.object_key, expires_in=24 * 3600, storage_config=storage.recording_config(recording))
        file_name = recording.file_name
        metadata = dict(job.metadata_json or {})
        speaker_count = _asr_speaker_count_from_metadata(metadata)
        speaker_mode = "settings" if speaker_count is None else "auto" if speaker_count == 0 else "fixed"
        metadata.update(
            {
                "asr_phase": "submitting",
                "asr_file_name": file_name,
                "asr_file_size_bytes": recording.file_size_bytes,
                "asr_speaker_count": speaker_count,
                "asr_speaker_mode": speaker_mode,
                "asr_queue_reason": "",
                "asr_next_retry_at": "",
            }
        )
        job.metadata_json = metadata
        job.status = "running"
        job.progress = max(job.progress or 0, 15)
        project_file = _asr_project_file(session, job, recording)
        recording.status = "asr_processing"
        if project_file:
            project_file.status = "asr_processing"
        expected_size = recording.file_size_bytes
    url_diagnostics = asr_client.inspect_audio_url(audio_url, expected_size=expected_size)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if job:
            metadata = dict(job.metadata_json or {})
            diagnostics = dict(metadata.get("asr_diagnostics") or {})
            diagnostics["audio_url_preflight"] = url_diagnostics
            metadata["asr_diagnostics"] = diagnostics
            job.metadata_json = metadata
        return audio_url, file_name, speaker_count, speaker_mode


def _save_asr_task_id(job_id: str, task_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        metadata = dict(job.metadata_json or {})
        now = datetime.now(timezone.utc)
        metadata.update(
            {
                "asr_phase": "polling",
                "asr_task_id": task_id,
                "asr_submitted_at": metadata.get("asr_submitted_at") or now.isoformat(),
                "asr_poll_count": int(metadata.get("asr_poll_count") or 0),
                "asr_last_status": "SUBMITTED",
            }
        )
        job.external_task_id = task_id
        job.metadata_json = metadata
        job.status = "running"
        job.progress = max(job.progress or 0, 20)


def _complete_asr_job(job_id: str, segments: list[dict]) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        if not recording:
            raise RuntimeError("RECORDING_NOT_FOUND")
        session.execute(delete(RawTranscriptSegment).where(RawTranscriptSegment.recording_id == recording.id))
        for item in segments:
            session.add(
                RawTranscriptSegment(
                    id=new_id("segraw"),
                    recording_id=recording.id,
                    speaker=item["speaker"],
                    start_time_ms=item["start_time_ms"],
                    end_time_ms=item["end_time_ms"],
                    text=item["text"],
                    confidence=item.get("confidence"),
                )
            )
        recording.status = "asr_completed"
        recording.duration_seconds = max([item["end_time_ms"] for item in segments] or [0]) // 1000
        project_file = _asr_project_file(session, job, recording)
        if project_file:
            project_file.status = "asr_completed"
            project_file.duration_seconds = recording.duration_seconds
        metadata = dict(job.metadata_json or {})
        metadata.update({"asr_phase": "completed", "asr_completed_at": datetime.now(timezone.utc).isoformat()})
        job.metadata_json = metadata
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                file_id=project_file.id if project_file else job.file_id,
                user_id=job.user_id,
                job_id=job.id,
                call_type="asr",
                model_provider="aliyun/mock",
                model_name=get_ai_config("asr").get("model", get_settings().asr_model),
                audio_duration_seconds=recording.duration_seconds,
                status="succeeded",
            )
        )
        next_job = create_job(session, recording.project_id, recording.id, "clean_transcript", {"file_id": project_file.id if project_file else job.file_id, "user_id": job.user_id})
        next_id = next_job.id
    enqueue_job(next_id)


def _run_asr_inline(job_id: str, record_asr_event) -> None:
    audio_url, file_name, speaker_count, speaker_mode = _prepare_asr_submission(job_id)

    def save_external_task_id(task_id: str) -> None:
        _save_asr_task_id(job_id, task_id)

    record_asr_event("download_url_created", {"file_name": file_name, "speaker_count": speaker_count, "speaker_mode": speaker_mode})
    segments = asr_client.transcribe(audio_url, file_name, speaker_count=speaker_count, on_task_id=save_external_task_id, on_event=record_asr_event)
    _complete_asr_job(job_id, segments)


def _submit_or_wait_asr(job_id: str, record_asr_event) -> None:
    lock = _acquire_redis_lock("asr:submit_capacity", timeout=180, blocking_timeout=5)
    if not lock:
        _mark_asr_waiting(job_id, "scheduler_busy", delay_seconds=5)
        return
    try:
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if not job or job.status in FINAL_JOB_STATUSES:
                return
            metadata = dict(job.metadata_json or {})
            existing_task_id = job.external_task_id or metadata.get("asr_task_id")
            if existing_task_id:
                enqueue_job(job_id, delay_seconds=_asr_delay_seconds(get_settings().asr_poll_interval_seconds))
                return
            counts = _asr_active_counts(session, job.user_id, job.id)
            wait = _asr_capacity_wait(counts, job.user_id)
        if wait:
            reason, detail = wait
            _mark_asr_waiting(job_id, reason, detail=detail)
            return

        audio_url, file_name, speaker_count, speaker_mode = _prepare_asr_submission(job_id)
        record_asr_event("download_url_created", {"file_name": file_name, "speaker_count": speaker_count, "speaker_mode": speaker_mode})
        try:
            task_id = asr_client.submit_task(audio_url, file_name, speaker_count=speaker_count, on_event=record_asr_event)
        except Exception as exc:
            if _is_asr_throttled_error(exc):
                _mark_asr_waiting(job_id, "asr_submit_throttled", detail={"error": str(exc)[:500]}, delay_seconds=60)
                return
            raise
        _save_asr_task_id(job_id, task_id)
    finally:
        _release_lock(lock)
    enqueue_job(job_id, delay_seconds=_asr_delay_seconds(get_settings().asr_poll_interval_seconds))


def _poll_submitted_asr(job_id: str, task_id: str, record_asr_event) -> None:
    settings = get_settings()
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        metadata = dict(job.metadata_json or {})
        submitted_at = _parse_utc_datetime(metadata.get("asr_submitted_at")) or job.started_at or datetime.now(timezone.utc)
        if datetime.now(timezone.utc) - submitted_at > timedelta(seconds=settings.asr_poll_timeout_seconds):
            raise RuntimeError(f"ASR_TASK_TIMEOUT: task_id={task_id}")
        poll_count = int(metadata.get("asr_poll_count") or 0) + 1
        metadata.update({"asr_phase": "polling", "asr_poll_count": poll_count})
        job.metadata_json = metadata
        job.progress = max(job.progress or 0, 20)
    try:
        result = asr_client.poll_task(task_id, on_event=record_asr_event, poll_count=poll_count)
    except Exception as exc:
        if not _is_asr_throttled_error(exc):
            raise
        delay = _asr_delay_seconds(60)
        next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if not job or job.status in FINAL_JOB_STATUSES:
                return
            metadata = dict(job.metadata_json or {})
            metadata.update(
                {
                    "asr_phase": "polling",
                    "asr_last_status": "THROTTLED",
                    "asr_next_poll_at": next_poll_at.isoformat(),
                    "asr_poll_count": poll_count,
                    "asr_poll_error": str(exc)[:500],
                }
            )
            job.metadata_json = metadata
            job.status = "running"
        enqueue_job(job_id, delay_seconds=delay)
        return
    if result.get("status") == "SUCCEEDED":
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if not job or job.status in FINAL_JOB_STATUSES:
                return
            metadata = dict(job.metadata_json or {})
            metadata.update({"asr_phase": "downloading", "asr_last_status": "SUCCEEDED"})
            job.metadata_json = metadata
            job.progress = max(job.progress or 0, 90)
        segments = asr_client.fetch_result_segments(str(result["result_url"]), task_id, on_event=record_asr_event)
        _complete_asr_job(job_id, segments)
        return

    delay = _asr_delay_seconds(settings.asr_poll_interval_seconds)
    next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        if not job or job.status in FINAL_JOB_STATUSES:
            return
        metadata = dict(job.metadata_json or {})
        metadata.update(
            {
                "asr_phase": "polling",
                "asr_last_status": result.get("status"),
                "asr_next_poll_at": next_poll_at.isoformat(),
                "asr_poll_count": poll_count,
            }
        )
        job.metadata_json = metadata
        job.status = "running"
        job.progress = min(89, max(job.progress or 20, 20 + poll_count))
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        project_file = _asr_project_file(session, job, recording)
        if recording:
            recording.status = "asr_processing"
        if project_file:
            project_file.status = "asr_processing"
    enqueue_job(job_id, delay_seconds=delay)


def _run_asr(job_id: str) -> None:
    def record_asr_event(event: str, payload: dict | None = None) -> None:
        with session_scope() as event_session:
            task_job = event_session.get(ProcessingJob, job_id)
            if not task_job:
                return
            metadata = dict(task_job.metadata_json or {})
            diagnostics = dict(metadata.get("asr_diagnostics") or {})
            events = list(diagnostics.get("events") or [])
            event_payload = payload or {}
            events.append(
                {
                    "event": event,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "payload": event_payload,
                }
            )
            diagnostics["events"] = events[-80:]
            diagnostics["last_event"] = event
            if event == "poll_status":
                diagnostics["last_status"] = event_payload.get("status")
                diagnostics["poll_count"] = event_payload.get("poll_count")
            elif event == "task_failed":
                diagnostics["last_status"] = event_payload.get("status")
                diagnostics["failure_detail"] = event_payload
                diagnostics["failure_hint"] = _asr_failure_hint(str(event_payload.get("message") or ""))
            metadata["asr_diagnostics"] = diagnostics
            task_job.metadata_json = metadata

    lock = _acquire_redis_lock(f"asr:job:{job_id}", timeout=300, blocking_timeout=1)
    if not lock:
        _reschedule_locked_asr(job_id)
        return
    try:
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if not job or job.status in FINAL_JOB_STATUSES:
                return
            metadata = dict(job.metadata_json or {})
            task_id = job.external_task_id or metadata.get("asr_task_id")
        if asr_client.use_local_mock():
            _run_asr_inline(job_id, record_asr_event)
            return
        if task_id:
            _poll_submitted_asr(job_id, str(task_id), record_asr_event)
            return
        _submit_or_wait_asr(job_id, record_asr_event)
    finally:
        _release_lock(lock)

def _run_clean(job_id: str) -> None:
    event_lock = Lock()

    def record_clean_event(event: str, payload: dict | None = None) -> None:
        with event_lock:
            with session_scope() as event_session:
                task_job = event_session.get(ProcessingJob, job_id)
                if not task_job:
                    return
                metadata = dict(task_job.metadata_json or {})
                diagnostics = dict(metadata.get("clean_diagnostics") or {})
                events = list(diagnostics.get("events") or [])
                event_payload = payload or {}
                events.append(
                    {
                        "event": event,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "payload": event_payload,
                    }
                )
                diagnostics["events"] = events[-120:]
                diagnostics["last_event"] = event
                if "batch_count" in event_payload:
                    diagnostics["batch_count"] = event_payload.get("batch_count")
                if event == "batch_plan":
                    diagnostics["segment_count"] = event_payload.get("segment_count")
                    diagnostics["max_workers"] = event_payload.get("max_workers")
                    task_job.progress = max(task_job.progress or 0, 15)
                elif event == "batch_completed":
                    completed = int(diagnostics.get("completed_batches") or 0) + 1
                    total = int(event_payload.get("batch_count") or diagnostics.get("batch_count") or completed)
                    diagnostics["completed_batches"] = completed
                    task_job.progress = min(95, 15 + int((completed / max(total, 1)) * 75))
                elif event == "batch_failed":
                    diagnostics["failed_batch_index"] = event_payload.get("batch_index")
                    diagnostics["failed_error"] = event_payload.get("error")
                elif event in {"mock_used", "all_batches_completed"}:
                    task_job.progress = max(task_job.progress or 0, 95)
                metadata["clean_diagnostics"] = diagnostics
                task_job.metadata_json = metadata

    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        raw = [
            {"id": seg.id, "speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}
            for seg in session.query(RawTranscriptSegment).filter_by(recording_id=recording.id).order_by(RawTranscriptSegment.start_time_ms).all()
        ]
    cleaned = llm_client.clean_segments(raw, on_event=record_clean_event)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        session.execute(delete(CleanTranscriptSegment).where(CleanTranscriptSegment.recording_id == recording.id))
        for item in cleaned:
            session.add(
                CleanTranscriptSegment(
                    id=new_id("seg"),
                    recording_id=recording.id,
                    raw_segment_id=item.get("raw_segment_id"),
                    speaker=item["speaker"],
                    start_time_ms=item["start_time_ms"],
                    end_time_ms=item["end_time_ms"],
                    text=item["clean_text"],
                    edited=False,
                )
            )
        recording.status = "cleaning_completed"
        project_file = session.query(ProjectFile).filter_by(recording_id=recording.id).first()
        if project_file:
            project_file.status = "cleaning_completed"
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                file_id=project_file.id if project_file else job.file_id,
                user_id=job.user_id,
                job_id=job.id,
                call_type="clean",
                model_provider="aliyun/mock",
                model_name=get_ai_config("clean").get("model", get_settings().llm_clean_model),
                input_tokens=sum(len(x["text"]) for x in raw),
                output_tokens=sum(len(x["clean_text"]) for x in cleaned),
                status="succeeded",
            )
        )
        next_job = create_job(session, recording.project_id, recording.id, "summary_generation", {"file_id": project_file.id if project_file else job.file_id, "user_id": job.user_id})
        next_id = next_job.id
    enqueue_job(next_id)


def _run_summary(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        segments = [
            {"id": seg.id, "speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}
            for seg in session.query(CleanTranscriptSegment).filter_by(recording_id=recording.id).order_by(CleanTranscriptSegment.start_time_ms).all()
        ]
        template_type = recording.template_type
    content = llm_client.summarize(segments, template_type)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        existing = session.query(SummaryArtifact).filter_by(recording_id=recording.id).first()
        if not existing:
            existing = SummaryArtifact(id=new_id("sum"), recording_id=recording.id)
            session.add(existing)
        existing.template_type = template_type
        existing.status = "ready"
        existing.stale = False
        existing.content = content
        recording.status = "completed"
        recording.summary_stale = False
        project_file = session.query(ProjectFile).filter_by(recording_id=recording.id).first()
        if project_file:
            project_file.status = "completed"
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                file_id=project_file.id if project_file else job.file_id,
                user_id=job.user_id,
                job_id=job.id,
                call_type="summary",
                model_provider="aliyun/mock",
                model_name=get_ai_config("summary").get("model", get_settings().llm_summary_model),
                input_tokens=sum(len(x["text"]) for x in segments),
                output_tokens=len(str(content)),
                status="succeeded",
            )
        )


def _run_extract_text(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        project_file = session.get(ProjectFile, job.file_id)
        if not project_file:
            raise RuntimeError("FILE_NOT_FOUND: 文件不存在")
        object_key = project_file.object_key
        file_config = storage.file_config(project_file)
        file_name = project_file.file_name
        extension = project_file.extension
        project_id = project_file.project_id
        created_by_id = project_file.created_by_id or job.user_id
        project_file.status = "extracting"
        project_file.extraction_status = "running"
        job.progress = 20
    content = storage.read_bytes(object_key, file_config)
    result = extract_content(file_name, extension, content)
    extracted_text = result.get("text", "") or ""
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        project_file = session.get(ProjectFile, job.file_id)
        if not project_file:
            raise RuntimeError("FILE_NOT_FOUND: 文件不存在")
        project_file.extracted_text = extracted_text
        project_file.extracted_char_count = len(extracted_text)
        project_file.extraction_engine = result.get("engine", "")
        project_file.extraction_warnings = result.get("warnings", [])
        project_file.extraction_status = "succeeded"
        project_file.status = "completed"
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=project_id,
                file_id=project_file.id,
                user_id=created_by_id,
                job_id=job.id,
                call_type="extract",
                model_provider="local",
                model_name=project_file.extraction_engine or "local-extractor",
                input_tokens=project_file.file_size_bytes or 0,
                output_tokens=project_file.extracted_char_count,
                status="succeeded",
            )
        )


def _run_qa(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        meta = job.metadata_json or {}
        thread = session.get(QAThread, meta.get("thread_id"))
        user_message = session.get(QAMessage, meta.get("user_message_id"))
        assistant_message = session.get(QAMessage, meta.get("assistant_message_id"))
        selected_file_ids = assistant_message.selected_file_ids or []
        selected_ids = assistant_message.selected_recording_ids or []
        recordings = session.query(Recording).filter(Recording.id.in_(selected_ids)).all()
        materials = []
        total_chars = 0
        for rec in recordings:
            segments = [
                {"speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}
                for seg in session.query(CleanTranscriptSegment).filter_by(recording_id=rec.id).order_by(CleanTranscriptSegment.start_time_ms).all()
            ]
            total_chars += sum(len(seg["text"]) for seg in segments)
            materials.append({"recording_id": rec.id, "file_name": rec.file_name, "segments": segments})
        for project_file in session.query(ProjectFile).filter(ProjectFile.id.in_(selected_file_ids), ProjectFile.file_type != "audio").all():
            total_chars += len(project_file.extracted_text or "")
            materials.append({"file_id": project_file.id, "file_name": project_file.file_name, "file_type": project_file.file_type, "text": project_file.extracted_text or ""})
        if total_chars > 100000:
            raise RuntimeError("LLM_CONTEXT_TOO_LONG")
        previous_messages = (
            session.query(QAMessage)
            .filter(QAMessage.thread_id == thread.id, QAMessage.id.notin_([user_message.id, assistant_message.id]))
            .order_by(QAMessage.created_at.desc())
            .limit(8)
            .all()
        )
        history = [
            {
                "role": msg.role,
                "content": msg.content,
                "selected_recording_ids": msg.selected_recording_ids,
            }
            for msg in reversed(previous_messages)
        ]
        question = user_message.content
    result = llm_client.answer(question, materials, history=history)
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        assistant_message = session.get(QAMessage, (job.metadata_json or {}).get("assistant_message_id"))
        thread = session.get(QAThread, (job.metadata_json or {}).get("thread_id"))
        assistant_message.content = result.get("answer_markdown") or result.get("answer", "")
        assistant_message.sources = result.get("sources") or [s for kp in result.get("key_points", []) for s in kp.get("sources", [])]
        assistant_message.usage = {"input_chars": total_chars, "model": get_ai_config("qa").get("model", get_settings().llm_qa_model)}
        assistant_message.status = "ready"
        thread.updated_at = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=thread.project_id,
                user_id=job.user_id,
                job_id=job.id,
                call_type="qa",
                model_provider="aliyun/mock",
                model_name=get_ai_config("qa").get("model", get_settings().llm_qa_model),
                input_tokens=total_chars,
                output_tokens=len(assistant_message.content),
                status="succeeded",
            )
        )


def _mark_succeeded(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)


def _error_code_for(job_type: str) -> str:
    return {
        "asr_transcription": "ASR_TASK_FAILED",
        "clean_transcript": "LLM_CLEAN_FAILED",
        "summary_generation": "LLM_SUMMARY_FAILED",
        "qa_answer": "LLM_CALL_FAILED",
        "extract_text": "TEXT_EXTRACTION_FAILED",
        "export": "EXPORT_FAILED",
    }.get(job_type, "INTERNAL_ERROR")
