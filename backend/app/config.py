from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    app_base_url: str = "http://localhost:8000"
    session_secret: str = "change-me"
    admin_username: str = "admin"
    admin_password: str = "mp2026"

    database_url: str = "sqlite:///./data/app.db"
    redis_url: str = "redis://localhost:6379/0"
    queue_sync: bool = False

    storage_provider: str = "railway_bucket"
    storage_mock_enabled: bool = False
    local_storage_dir: str = "./local_storage"
    bucket: str = ""
    endpoint: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = ""
    railway_bucket_endpoint: str = ""
    railway_bucket_name: str = ""
    railway_bucket_access_key_id: str = ""
    railway_bucket_secret_access_key: str = ""
    railway_bucket_region: str = "auto"
    storage_path_prefix: str = ""
    storage_addressing_style: str = "virtual"

    asr_mock_enabled: bool = True
    asr_api_url: str = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    asr_api_key: str = ""
    asr_model: str = "fun-asr"
    asr_poll_interval_seconds: int = 10
    asr_poll_timeout_seconds: int = 14400
    asr_diarization_enabled: bool = True
    asr_speaker_count: int = 2

    llm_mock_enabled: bool = True
    llm_clean_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_clean_api_key: str = ""
    llm_clean_model: str = "qwen3.5-flash"
    llm_summary_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_summary_api_key: str = ""
    llm_summary_model: str = "qwen3.5-flash"
    llm_qa_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_qa_api_key: str = ""
    llm_qa_model: str = "qwen3.6-plus"
    llm_timeout_seconds: int = 600
    llm_clean_batch_max_segments: int = 40
    llm_clean_batch_max_chars: int = 12000
    llm_clean_batch_concurrency: int = 3

    max_upload_size_mb: int = 500
    max_recording_duration_hours: int = 3
    max_qa_recordings: int = 10
    qa_overflow_strategy: str = "reject"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def local_storage_path(self) -> Path:
        return Path(self.local_storage_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
