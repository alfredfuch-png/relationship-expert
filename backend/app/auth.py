from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, Response

from app.settings import Settings, get_settings
from app.users_store import User, has_users, verify_user_password

COOKIE_NAME = "re_session"
SESSION_MARKER = b"romance-expert-authenticated"
SESSION_V2_PREFIX = "v2."


AuthMode = Literal["none", "shared_password", "accounts"]


def auth_mode(settings: Settings | None = None) -> AuthMode:
    settings = settings or get_settings()
    if has_users(settings):
        return "accounts"
    if settings.app_password.strip():
        return "shared_password"
    return "none"


def auth_enabled(settings: Settings | None = None) -> bool:
    return auth_mode(settings) != "none"


def server_chat_enabled(settings: Settings | None = None) -> bool:
    return auth_mode(settings) == "accounts"


def _session_secret(settings: Settings) -> str:
    if settings.session_secret.strip():
        return settings.session_secret.strip()
    if settings.app_password.strip():
        digest = hashlib.sha256(
            (settings.app_password + ":romance-expert-session").encode()
        ).hexdigest()
        return digest
    if has_users(settings):
        digest = hashlib.sha256(b"romance-expert-users-session").hexdigest()
        return digest
    return ""


def _legacy_session_token(settings: Settings) -> str:
    secret = _session_secret(settings)
    if not secret:
        return ""
    return hmac.new(secret.encode(), SESSION_MARKER, hashlib.sha256).hexdigest()


def _sign_user_session(user_id: str, settings: Settings) -> str:
    secret = _session_secret(settings)
    if not secret:
        return ""
    sig = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{SESSION_V2_PREFIX}{user_id}.{sig}"


def _verify_user_session(token: str, settings: Settings) -> str | None:
    if not token.startswith(SESSION_V2_PREFIX):
        return None
    rest = token[len(SESSION_V2_PREFIX) :]
    if "." not in rest:
        return None
    user_id, sig = rest.split(".", 1)
    secret = _session_secret(settings)
    if not secret or not user_id or not sig:
        return None
    expected = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


def verify_shared_password(candidate: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    expected = settings.app_password
    if not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def authenticate_login(
    *,
    username: str | None,
    password: str,
    settings: Settings | None = None,
) -> User | None:
    settings = settings or get_settings()
    mode = auth_mode(settings)
    if mode == "none":
        return None
    if mode == "shared_password":
        return User(id="shared", username="访客") if verify_shared_password(password, settings) else None
    user = verify_user_password(username or "", password, settings)
    return user


def resolve_user_id_from_request(request: Request, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if not auth_enabled(settings):
        return "anonymous"
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    mode = auth_mode(settings)
    if mode == "accounts":
        return _verify_user_session(cookie, settings)
    if mode == "shared_password":
        expected = _legacy_session_token(settings)
        if expected and hmac.compare_digest(cookie, expected):
            return "shared"
        return None
    return None


def resolve_user(request: Request, settings: Settings | None = None) -> User | None:
    settings = settings or get_settings()
    user_id = resolve_user_id_from_request(request, settings)
    if not user_id:
        return None
    if user_id == "anonymous":
        return User(id="anonymous", username="本地")
    if user_id == "shared":
        return User(id="shared", username="访客")
    from app.users_store import get_user_by_id

    return get_user_by_id(user_id, settings)


def set_session_cookie(response: Response, user: User, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    mode = auth_mode(settings)
    if mode == "accounts":
        token = _sign_user_session(user.id, settings)
    else:
        token = _legacy_session_token(settings)
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


def require_user_id(request: Request) -> str:
    settings = get_settings()
    if not auth_enabled(settings):
        return "anonymous"
    user_id = resolve_user_id_from_request(request, settings)
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user_id


CurrentUserId = Annotated[str, Depends(require_user_id)]
