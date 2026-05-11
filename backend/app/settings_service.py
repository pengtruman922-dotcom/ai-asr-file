from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .config import get_settings
from .database import session_scope
from .models import SystemSetting

SETTINGS_KEY = "app"
AI_NODES = ("asr", "clean", "summary", "qa")
STORAGE_PROVIDERS = ("local", "railway_bucket", "s3_compatible")


def _positive_int(value: Any, fallback: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(parsed, minimum)


def default_app_settings() -> dict:
    env = get_settings()
    return {
        "basic": {
            "max_upload_size_mb": env.max_upload_size_mb,
            "max_recording_duration_hours": env.max_recording_duration_hours,
        },
        "ai": {
            "asr": {"model": env.asr_model, "url": env.asr_api_url, "api_key": env.asr_api_key},
            "clean": {"model": env.llm_clean_model, "url": env.llm_clean_base_url, "api_key": env.llm_clean_api_key},
            "summary": {"model": env.llm_summary_model, "url": env.llm_summary_base_url, "api_key": env.llm_summary_api_key},
            "qa": {"model": env.llm_qa_model, "url": env.llm_qa_base_url, "api_key": env.llm_qa_api_key},
        },
        "storage": {
            "provider": env.storage_provider,
            "bucket_name": env.railway_bucket_name,
            "endpoint": env.railway_bucket_endpoint,
            "region": env.railway_bucket_region,
            "access_key_id": env.railway_bucket_access_key_id,
            "secret_access_key": env.railway_bucket_secret_access_key,
            "path_prefix": env.storage_path_prefix,
        },
    }


def _normalize(payload: dict | None, base: dict) -> dict:
    payload = payload or {}
    merged = deepcopy(base)

    basic = payload.get("basic") if isinstance(payload.get("basic"), dict) else {}
    merged["basic"]["max_upload_size_mb"] = _positive_int(
        basic.get("max_upload_size_mb", basic.get("max_size_mb")),
        merged["basic"]["max_upload_size_mb"],
    )
    merged["basic"]["max_recording_duration_hours"] = _positive_int(
        basic.get("max_recording_duration_hours", basic.get("max_duration_hours")),
        merged["basic"]["max_recording_duration_hours"],
    )

    ai = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
    for node in AI_NODES:
        incoming = ai.get(node) if isinstance(ai.get(node), dict) else {}
        current = merged["ai"][node]
        if "model" in incoming and str(incoming.get("model") or "").strip():
            current["model"] = str(incoming["model"]).strip()
        if "url" in incoming:
            current["url"] = str(incoming.get("url") or "").strip()
        key_value = incoming.get("api_key", incoming.get("key"))
        if incoming.get("clear_key") is True:
            current["api_key"] = ""
        elif key_value is not None and str(key_value).strip():
            current["api_key"] = str(key_value).strip()

    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    current_storage = merged["storage"]
    provider = str(storage.get("provider") or current_storage.get("provider") or "local").strip()
    current_storage["provider"] = provider if provider in STORAGE_PROVIDERS else "local"
    for field in ("bucket_name", "endpoint", "region", "path_prefix"):
        if field in storage:
            current_storage[field] = str(storage.get(field) or "").strip()
    access_key = storage.get("access_key_id", storage.get("access_key"))
    secret_key = storage.get("secret_access_key", storage.get("secret_key"))
    if storage.get("clear_access_key") is True:
        current_storage["access_key_id"] = ""
    elif access_key is not None and str(access_key).strip():
        current_storage["access_key_id"] = str(access_key).strip()
    if storage.get("clear_secret_key") is True:
        current_storage["secret_access_key"] = ""
    elif secret_key is not None and str(secret_key).strip():
        current_storage["secret_access_key"] = str(secret_key).strip()

    return merged


def get_app_settings(session: Session | None = None) -> dict:
    if session is None:
        with session_scope() as scoped_session:
            return get_app_settings(scoped_session)

    base = default_app_settings()
    row = session.get(SystemSetting, SETTINGS_KEY)
    if not row or not isinstance(row.value, dict):
        return base
    return _normalize(row.value, base)


def save_app_settings(session: Session, payload: dict) -> dict:
    current = get_app_settings(session)
    updated = _normalize(payload, current)
    row = session.get(SystemSetting, SETTINGS_KEY)
    if not row:
        row = SystemSetting(key=SETTINGS_KEY, value=updated)
        session.add(row)
    else:
        row.value = updated
        row.updated_at = datetime.now(timezone.utc)
    session.flush()
    return updated


def public_app_settings(session: Session) -> dict:
    settings = get_app_settings(session)
    public_ai = {}
    for node, config in settings["ai"].items():
        public_ai[node] = {
            "model": config.get("model", ""),
            "url": config.get("url", ""),
            "key": "",
            "key_configured": bool(config.get("api_key")),
        }
    storage = settings.get("storage", {})
    public_storage = {
        "provider": storage.get("provider", "local"),
        "bucket_name": storage.get("bucket_name", ""),
        "endpoint": storage.get("endpoint", ""),
        "region": storage.get("region", "auto"),
        "path_prefix": storage.get("path_prefix", ""),
        "access_key_id": "",
        "secret_access_key": "",
        "access_key_configured": bool(storage.get("access_key_id")),
        "secret_key_configured": bool(storage.get("secret_access_key")),
    }
    return {"basic": settings["basic"], "ai": public_ai, "storage": public_storage}


def get_ai_config(node: str) -> dict:
    settings = get_app_settings()
    return settings["ai"].get(node, {})


def get_basic_config(session: Session | None = None) -> dict:
    return get_app_settings(session).get("basic", {})


def get_storage_config(session: Session | None = None) -> dict:
    return get_app_settings(session).get("storage", {})


def resolve_storage_config(session: Session, payload: dict | None = None) -> dict:
    current = get_app_settings(session)
    merged = _normalize({"storage": payload or {}}, current)
    return merged.get("storage", {})
