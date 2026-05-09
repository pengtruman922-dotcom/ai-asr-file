from datetime import datetime, timezone

from sqlalchemy import delete

from .clients import asr_client, llm_client
from .config import get_settings
from .database import session_scope
from .settings_service import get_ai_config
from .models import (
    CleanTranscriptSegment,
    ProcessingJob,
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


def enqueue_job(job_id: str) -> None:
    settings = get_settings()
    if settings.app_env == "local" and settings.queue_sync:
        run_job(job_id)
        return
    from redis import Redis
    from rq import Queue

    queue = Queue("default", connection=Redis.from_url(settings.redis_url))
    queue.enqueue("app.tasks.run_job", job_id, job_timeout="6h")


def create_job(session, project_id: str | None, recording_id: str | None, job_type: str, metadata: dict | None = None) -> ProcessingJob:
    job = ProcessingJob(
        id=new_id("job"),
        project_id=project_id,
        recording_id=recording_id,
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
        if recording and job.job_type == "asr_transcription":
            recording.status = "asr_processing"
        elif recording and job.job_type == "clean_transcript":
            recording.status = "cleaning"
        elif recording and job.job_type == "summary_generation":
            recording.status = "summary_generating"
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
        new_id_value = new.id
    enqueue_job(new_id_value)
    return new_id_value


def _run_asr(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        audio_url = storage.create_download_url(recording.object_key, expires_in=24 * 3600, storage_config=storage.recording_config(recording))
        file_name = recording.file_name

    def save_external_task_id(task_id: str) -> None:
        with session_scope() as task_session:
            task_job = task_session.get(ProcessingJob, job_id)
            if task_job:
                task_job.external_task_id = task_id
                task_job.metadata_json = {**(task_job.metadata_json or {}), "asr_task_id": task_id}

    segments = asr_client.transcribe(audio_url, file_name, on_task_id=save_external_task_id)

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
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                job_id=job.id,
                call_type="asr",
                model_provider="aliyun/mock",
                model_name=get_ai_config("asr").get("model", get_settings().asr_model),
                audio_duration_seconds=recording.duration_seconds,
                status="succeeded",
            )
        )
        next_job = create_job(session, recording.project_id, recording.id, "clean_transcript")
        next_id = next_job.id
    enqueue_job(next_id)


def _run_clean(job_id: str) -> None:
    with session_scope() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, job.recording_id)
        raw = [
            {"id": seg.id, "speaker": seg.speaker, "start_time_ms": seg.start_time_ms, "end_time_ms": seg.end_time_ms, "text": seg.text}
            for seg in session.query(RawTranscriptSegment).filter_by(recording_id=recording.id).order_by(RawTranscriptSegment.start_time_ms).all()
        ]
    cleaned = llm_client.clean_segments(raw)
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
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                job_id=job.id,
                call_type="clean",
                model_provider="aliyun/mock",
                model_name=get_ai_config("clean").get("model", get_settings().llm_clean_model),
                input_tokens=sum(len(x["text"]) for x in raw),
                output_tokens=sum(len(x["clean_text"]) for x in cleaned),
                status="succeeded",
            )
        )
        next_job = create_job(session, recording.project_id, recording.id, "summary_generation")
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
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        session.add(
            UsageRecord(
                id=new_id("use"),
                project_id=recording.project_id,
                recording_id=recording.id,
                job_id=job.id,
                call_type="summary",
                model_provider="aliyun/mock",
                model_name=get_ai_config("summary").get("model", get_settings().llm_summary_model),
                input_tokens=sum(len(x["text"]) for x in segments),
                output_tokens=len(str(content)),
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
        "export": "EXPORT_FAILED",
    }.get(job_type, "INTERNAL_ERROR")
