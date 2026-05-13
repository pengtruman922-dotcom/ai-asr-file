from datetime import datetime, timezone
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


def queue_name_for_job_type(job_type: str) -> str:
    settings = get_settings()
    if settings.task_queue_routing.strip().lower() == "split":
        return JOB_QUEUE_MAP.get(job_type, "default")
    return "default"


def enqueue_job(job_id: str) -> None:
    settings = get_settings()
    if settings.app_env == "local" and settings.queue_sync:
        run_job(job_id)
        return
    from redis import Redis
    from rq import Queue

    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        queue_name = queue_name_for_job_type(job.job_type) if job else "default"

    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))
    queue.enqueue("app.tasks.run_job", job_id, job_timeout="6h")


def create_job(session, project_id: str | None, recording_id: str | None, job_type: str, metadata: dict | None = None) -> ProcessingJob:
    job = ProcessingJob(
        id=new_id("job"),
        project_id=project_id,
        recording_id=recording_id,
        file_id=(metadata or {}).get("file_id"),
        user_id=(metadata or {}).get("user_id"),
        job_type=job_type,
        status="queued",
        metadata_json=metadata or {},
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
        job_type = job.job_type
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress = 10
        recording = session.get(Recording, job.recording_id) if job.recording_id else None
        project_file = session.get(ProjectFile, job.file_id) if job.file_id else None
        if recording and job.job_type == "asr_transcription":
            recording.status = "asr_processing"
            if project_file:
                project_file.status = "asr_processing"
        elif recording and job.job_type == "clean_transcript":
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
                job.status = "failed"
                job.error_code = _error_code_for(job_type)
                job.error_message = str(exc)
                job.finished_at = datetime.now(timezone.utc)
                job.progress = 100
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


def retry_failed_job(job_id: str) -> str:
    with session_scope() as session:
        old = session.get(ProcessingJob, job_id)
        if not old or old.status != "failed":
            raise ValueError("JOB_NOT_RETRYABLE")
        new = create_job(session, old.project_id, old.recording_id, old.job_type, old.metadata_json)
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


def _asr_speaker_count_from_metadata(metadata: dict | None) -> int | None:
    if not metadata or "asr_speaker_count" not in metadata:
        return None
    try:
        value = int(metadata.get("asr_speaker_count") or 0)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 2, 3, 4} else None


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
            metadata["asr_diagnostics"] = diagnostics
            task_job.metadata_json = metadata

    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        audio_url = storage.create_download_url(recording.object_key, expires_in=24 * 3600, storage_config=storage.recording_config(recording))
        file_name = recording.file_name
        metadata = dict(job.metadata_json or {})
        speaker_count = _asr_speaker_count_from_metadata(metadata)
        speaker_mode = "settings" if speaker_count is None else "auto" if speaker_count == 0 else "fixed"
        job.metadata_json = {
            **metadata,
            "asr_file_name": file_name,
            "asr_file_size_bytes": recording.file_size_bytes,
            "asr_speaker_count": speaker_count,
            "asr_speaker_mode": speaker_mode,
        }

    def save_external_task_id(task_id: str) -> None:
        with session_scope() as task_session:
            task_job = task_session.get(ProcessingJob, job_id)
            if task_job:
                task_job.external_task_id = task_id
                task_job.metadata_json = {**(task_job.metadata_json or {}), "asr_task_id": task_id}

    record_asr_event("download_url_created", {"file_name": file_name, "speaker_count": speaker_count, "speaker_mode": speaker_mode})
    segments = asr_client.transcribe(audio_url, file_name, speaker_count=speaker_count, on_task_id=save_external_task_id, on_event=record_asr_event)

    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
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
        project_file = session.query(ProjectFile).filter_by(recording_id=recording.id).first()
        if project_file:
            project_file.status = "asr_completed"
            project_file.duration_seconds = recording.duration_seconds
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
