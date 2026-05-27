from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Response

from app.settings import Settings, get_settings

COOKIE_NAME = "re_session"
SESSION_MARKER = b"romance-expert-authenticated"


def auth_enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.app_password.strip())


def _session_secret(settings: Settings) -> str:
    if settings.session_secret.strip():
        return settings.session_secret.strip()
    if settings.app_password.strip():
        digest = hashlib.sha256(
            (settings.app_password + ":romance-expert-session").encode()
        ).hexdigest()
        return digest
    return ""


def _session_token(settings: Settings) -> str:
    secret = _session_secret(settings)
    if not secret:
        return ""
    return hmac.new(secret.encode(), SESSION_MARKER, hashlib.sha256).hexdigest()


def verify_password(candidate: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    expected = settings.app_password
    if not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def verify_session(cookie_value: str | None, settings: Settings | None = None) -> bool:
    if not auth_enabled(settings):
        return True
    if not cookie_value:
        return False
    settings = settings or get_settings()
    expected = _session_token(settings)
    if not expected:
        return False
    return hmac.compare_digest(cookie_value, expected)


def set_session_cookie(response: Response, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    token = _session_token(settings)
    secure = settings.public_deploy or settings.cookie_secure
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=60 * 60 * 24 * 14,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    secure = settings.public_deploy or settings.cookie_secure
    response.delete_cookie(key=COOKIE_NAME, path="/", secure=secure, samesite="lax")


def require_session(request: Request) -> None:
    settings = get_settings()
    if not auth_enabled(settings):
        return
    if verify_session(request.cookies.get(COOKIE_NAME), settings):
        return
    raise HTTPException(status_code=401, detail="Login required.")


SessionUser = Annotated[None, "Authenticated session"]
