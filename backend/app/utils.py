import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .config import get_settings


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ok(data: Any = None):
    return {"success": True, "data": data if data is not None else {}, "server_time": now_iso()}


def fail(code: str, message: str, status_code: int = 400, details: dict | None = None):
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
            "server_time": now_iso(),
        },
    )


def make_token(username: str) -> str:
    settings = get_settings()
    exp = int(time.time()) + 7 * 24 * 3600
    payload = json.dumps({"u": username, "exp": exp}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str) -> str | None:
    settings = get_settings()
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload.get("u")
    except Exception:
        return None


def require_auth(request: Request) -> str:
    path = request.url.path
    if path in ["/api/health", "/api/auth/login"] or path.startswith("/api/mock-storage/"):
        return "anonymous"
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    username = verify_token(header.removeprefix("Bearer ").strip())
    if not username:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return username


def serialize_dt(value):
    return value.isoformat() if value else None
