from pathlib import Path
import shutil
import uuid

import boto3
from botocore.client import Config
from boto3.s3.transfer import TransferConfig

from .config import get_settings
from .settings_service import get_storage_config


class StorageService:
    SMALL_FILE_MULTIPART_THRESHOLD = 64 * 1024 * 1024

    def __init__(self):
        self.settings = get_settings()
        self.local_root = self.settings.local_storage_path
        self.local_root.mkdir(parents=True, exist_ok=True)

    def _local_path(self, object_key: str) -> Path:
        safe_key = object_key.replace("..", "").lstrip("/\\")
        return self.local_root / safe_key

    def _effective_config(self, storage_config: dict | None = None) -> dict:
        # Start with current settings so saved credentials can be reused for file snapshots.
        config = dict(get_storage_config() or {})
        if storage_config:
            config.update(storage_config)

        if self.settings.app_env != "local" and config.get("provider") == "local":
            config["provider"] = "railway_bucket"
        if not config.get("provider"):
            config["provider"] = self.settings.storage_provider
        if not config.get("bucket_name"):
            config["bucket_name"] = self.settings.bucket or self.settings.railway_bucket_name
        if not config.get("endpoint"):
            config["endpoint"] = self.settings.endpoint or self.settings.railway_bucket_endpoint
        if not config.get("region"):
            config["region"] = self.settings.region or self.settings.railway_bucket_region
        if not config.get("access_key_id"):
            config["access_key_id"] = self.settings.access_key_id or self.settings.railway_bucket_access_key_id
        if not config.get("secret_access_key"):
            config["secret_access_key"] = self.settings.secret_access_key or self.settings.railway_bucket_secret_access_key
        if not config.get("path_prefix"):
            config["path_prefix"] = self.settings.storage_path_prefix
        if not config.get("addressing_style"):
            config["addressing_style"] = self.settings.storage_addressing_style
        return config

    def _is_local(self, storage_config: dict | None = None) -> bool:
        return self.settings.app_env == "local" and self._effective_config(storage_config).get("provider") == "local"

    def _full_key(self, object_key: str, storage_config: dict | None = None) -> str:
        config = self._effective_config(storage_config)
        prefix = str(config.get("path_prefix") or "").strip().strip("/")
        clean_key = object_key.replace("..", "").lstrip("/\\")
        return f"{prefix}/{clean_key}" if prefix else clean_key

    def recording_config(self, recording) -> dict:
        return {
            "provider": recording.storage_provider or "local",
            "bucket_name": recording.storage_bucket_name or "",
            "endpoint": recording.storage_endpoint or "",
            "region": recording.storage_region or "auto",
            "path_prefix": recording.storage_path_prefix or "",
        }

    def file_config(self, project_file) -> dict:
        return {
            "provider": project_file.storage_provider or "local",
            "bucket_name": project_file.storage_bucket_name or "",
            "endpoint": project_file.storage_endpoint or "",
            "region": project_file.storage_region or "auto",
            "path_prefix": project_file.storage_path_prefix or "",
        }

    def create_upload_url(self, object_key: str, content_type: str, storage_config: dict | None = None):
        full_key = self._full_key(object_key, storage_config)
        if self._is_local(storage_config):
            return {
                "method": "PUT",
                "url": f"{self.settings.app_base_url}/api/mock-storage/{full_key}",
                "headers": {"Content-Type": content_type or "application/octet-stream"},
                "expires_in_seconds": 3600,
            }
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        url = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": config["bucket_name"], "Key": full_key, "ContentType": content_type},
            ExpiresIn=3600,
        )
        return {"method": "PUT", "url": url, "headers": {"Content-Type": content_type}, "expires_in_seconds": 3600}

    def upload_fileobj(self, object_key: str, file_obj, content_type: str, storage_config: dict | None = None) -> None:
        full_key = self._full_key(object_key, storage_config)
        file_obj.seek(0)
        if self._is_local(storage_config):
            path = self._local_path(full_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as target:
                shutil.copyfileobj(file_obj, target, length=1024 * 1024)
            return
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        client.upload_fileobj(
            file_obj,
            config["bucket_name"],
            full_key,
            ExtraArgs={"ContentType": content_type or "application/octet-stream"},
            Config=TransferConfig(multipart_threshold=self.SMALL_FILE_MULTIPART_THRESHOLD),
        )

    def create_download_url(self, object_key: str, expires_in: int = 3600, storage_config: dict | None = None):
        full_key = self._full_key(object_key, storage_config)
        if self._is_local(storage_config):
            return f"{self.settings.app_base_url}/api/mock-storage/{full_key}"
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": config["bucket_name"], "Key": full_key},
            ExpiresIn=expires_in,
        )

    def save_local_bytes(self, object_key: str, content: bytes):
        path = self._local_path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def save_text(self, object_key: str, content: str, storage_config: dict | None = None):
        full_key = self._full_key(object_key, storage_config)
        if self._is_local(storage_config):
            path = self._local_path(full_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        client.put_object(
            Bucket=config["bucket_name"],
            Key=full_key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def read_bytes(self, object_key: str, storage_config: dict | None = None) -> bytes:
        full_key = self._full_key(object_key, storage_config)
        if self._is_local(storage_config):
            return self._local_path(full_key).read_bytes()
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        response = client.get_object(Bucket=config["bucket_name"], Key=full_key)
        return response["Body"].read()

    def delete_prefix(self, prefix: str, storage_config: dict | None = None):
        full_prefix = self._full_key(prefix, storage_config)
        if self._is_local(storage_config):
            path = self._local_path(full_prefix)
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                for item in self.local_root.glob(full_prefix.rstrip("/") + "*"):
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            return
        config = self._effective_config(storage_config)
        self._validate_remote_config(config)
        client = self._s3_client(config)
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config["bucket_name"], Prefix=full_prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                client.delete_objects(Bucket=config["bucket_name"], Delete={"Objects": objects})

    def test_connection(self, storage_config: dict | None = None) -> dict:
        config = self._effective_config(storage_config)
        provider = config.get("provider") or "local"
        if provider == "local" and self.settings.app_env == "local":
            self.local_root.mkdir(parents=True, exist_ok=True)
            return {"status": "passed", "message": f"本地存储可用：{self.local_root}"}
        self._validate_remote_config(config)
        client = self._s3_client(config)
        key = self._full_key(f"_healthchecks/{uuid.uuid4().hex}.txt", config)
        bucket = config["bucket_name"]
        client.put_object(Bucket=bucket, Key=key, Body=b"ok", ContentType="text/plain")
        client.head_object(Bucket=bucket, Key=key)
        client.delete_object(Bucket=bucket, Key=key)
        return {"status": "passed", "message": "Bucket 连接成功，已完成写入/读取/删除测试"}

    def _validate_remote_config(self, config: dict) -> None:
        missing = [field for field in ["endpoint", "bucket_name", "access_key_id", "secret_access_key"] if not config.get(field)]
        if missing:
            raise RuntimeError(
                "STORAGE_CONFIG_MISSING: Railway Bucket 配置不完整，缺少 "
                + ", ".join(missing)
                + "。请确认 Web/Worker 服务已引用 Bucket 变量，或在系统设置中保存 Bucket 配置。"
            )

    def _s3_client(self, storage_config: dict | None = None):
        config = self._effective_config(storage_config)
        return boto3.client(
            "s3",
            endpoint_url=config.get("endpoint") or None,
            aws_access_key_id=config.get("access_key_id"),
            aws_secret_access_key=config.get("secret_access_key"),
            region_name=config.get("region") or "auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": config.get("addressing_style") or "virtual"}),
        )


storage = StorageService()
