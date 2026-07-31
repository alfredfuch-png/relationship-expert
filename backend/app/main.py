from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.auth import (
    auth_enabled,
    auth_mode,
    authenticate_login,
    clear_session_cookie,
    CurrentAdminUser,
    CurrentUserId,
    registration_enabled,
    resolve_user,
    server_chat_enabled,
    set_session_cookie,
    verify_registration_invite,
)
from app.chat import (
    build_consult_chat_messages,
    effective_chat_model,
    filter_relevant_chunks,
    stream_chat_completion,
    uses_kimi_chat,
)
from app.consult import (
    decide_phase,
    load_questions_guide,
    refresh_context_summary,
    refresh_user_memory,
    should_update_user_memory,
)
from app.indexing import read_index_meta, rebuild_index_async
from app.retrieve import retrieve_context
from app.settings import _project_root, get_settings
from app.startup import prepare_runtime_data
from app.users_db_sync import r2_sync_configured, schedule_users_db_sync, sync_secret, sync_users_db_to_r2
from app.users_store import (
    RegistrationInviteLimitError,
    TOKEN_QUOTA_EXCEEDED_MESSAGE,
    admin_list_users,
    admin_overview,
    admin_user_detail,
    admin_user_usage,
    clear_user_memory,
    consume_registration_slot,
    create_user,
    get_token_quota,
    load_chat_state,
    load_user_memory,
    record_usage_event,
    registration_slots_remaining,
    release_registration_slot,
    save_chat_state,
    save_user_memory,
)


def _web_dist() -> Path:
    return _project_root() / "web" / "dist"


def _cors_origins() -> list[str]:
    s = get_settings()
    extra = os.getenv("CORS_ORIGINS", "")
    origins = [
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    if extra.strip():
        origins.extend(x.strip() for x in extra.split(",") if x.strip())
    if s.public_deploy:
        origins.append("https://relationship-expert.ai-builders.space")
    return origins


@asynccontextmanager
async def lifespan(_app: FastAPI):
    prepare_runtime_data()
    yield
    try:
        sync_users_db_to_r2(get_settings(), force=True)
    except Exception:
        pass


app = FastAPI(title="Romance Expert RAG", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=12000)


class ChatImage(BaseModel):
    mime: str = Field(default="image/jpeg", max_length=64)
    data_base64: str = Field(min_length=32, max_length=5_500_000)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=8000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=40)
    context_summary: str = Field(default="", max_length=8000)
    images: list[ChatImage] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def require_text_or_images(self) -> ChatBody:
        if not self.message.strip() and not self.images:
            raise ValueError("message or images required")
        for img in self.images:
            mime = img.mime.lower().strip()
            if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                raise ValueError(f"unsupported image mime: {img.mime}")
        return self


class LoginBody(BaseModel):
    username: str = Field(default="", max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=4, max_length=256)
    invite_code: str = Field(min_length=1, max_length=128)


class ChatStateBody(BaseModel):
    threads: list[dict]
    active_id: str | None = None


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    s = get_settings()
    mode = auth_mode(s)
    enabled = auth_enabled(s)
    user = resolve_user(request, s) if enabled else None
    return {
        "auth_required": enabled,
        "authenticated": user is not None if enabled else True,
        "auth_mode": mode,
        "username": user.username if user else None,
        "is_admin": bool(user.is_admin) if user else False,
        "server_chat": server_chat_enabled(s),
        "registration_enabled": registration_enabled(s),
        "registration_slots_remaining": registration_slots_remaining(s)
        if registration_enabled(s)
        else None,
    }


@app.post("/api/auth/login")
def auth_login(body: LoginBody, response: Response) -> dict:
    s = get_settings()
    mode = auth_mode(s)
    if not auth_enabled(s):
        return {"ok": True, "auth_required": False, "auth_mode": "none", "is_admin": False}
    user = authenticate_login(username=body.username, password=body.password, settings=s)
    if not user:
        if mode == "accounts":
            raise HTTPException(status_code=401, detail="账户名或密码错误")
        raise HTTPException(status_code=401, detail="密码错误")
    set_session_cookie(response, user, s)
    return {
        "ok": True,
        "auth_required": True,
        "auth_mode": mode,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "server_chat": server_chat_enabled(s),
    }


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/register")
def auth_register(
    body: RegisterBody,
    response: Response,
    background_tasks: BackgroundTasks,  # noqa: ARG001 — kept for API stability
) -> dict:
    s = get_settings()
    if not registration_enabled(s):
        raise HTTPException(status_code=403, detail="注册未开放。")
    if not verify_registration_invite(body.invite_code, s):
        raise HTTPException(status_code=403, detail="邀请码无效。")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="账户名不能为空。")
    max_uses = s.registration_invite_max_uses
    try:
        consume_registration_slot(s)
    except RegistrationInviteLimitError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"邀请码已达使用上限（{max_uses} 次）。",
        ) from exc
    try:
        user = create_user(username, body.password, s)
    except ValueError as exc:
        release_registration_slot(s)
        msg = str(exc)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail="该账户名已被使用。") from exc
        if "too short" in msg:
            raise HTTPException(status_code=400, detail="密码至少 4 个字符。") from exc
        raise HTTPException(status_code=400, detail="无法创建账户。") from exc
    except Exception:
        release_registration_slot(s)
        raise
    set_session_cookie(response, user, s)
    backed_up = False
    if r2_sync_configured(s):
        try:
            backed_up = sync_users_db_to_r2(s, force=True)
        except Exception:
            backed_up = False
    return {
        "ok": True,
        "auth_required": True,
        "auth_mode": auth_mode(s),
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "server_chat": server_chat_enabled(s),
        "users_backed_up": backed_up,
    }


@app.get("/api/config")
def public_config(request: Request, user_id: CurrentUserId) -> dict:  # noqa: ARG001
    s = get_settings()
    user = resolve_user(request, s)
    return {
        "public_deploy": s.public_deploy,
        "show_sources": not s.public_deploy,
        "show_routing": not s.public_deploy,
        "allow_index": not s.public_deploy,
        "auth_required": auth_enabled(s),
        "auth_mode": auth_mode(s),
        "server_chat": server_chat_enabled(s),
        "username": user.username if user else None,
        "is_admin": bool(user.is_admin) if user else False,
    }


@app.get("/api/chat/state")
def get_chat_state(user_id: CurrentUserId) -> dict:
    if not server_chat_enabled():
        raise HTTPException(status_code=404, detail="Server chat storage is not enabled.")
    if user_id in ("anonymous", "shared"):
        return {"threads": [], "active_id": None}
    state = load_chat_state(user_id) or {"threads": [], "active_id": None}
    return state


@app.put("/api/chat/state")
def put_chat_state(
    body: ChatStateBody,
    user_id: CurrentUserId,
    background_tasks: BackgroundTasks,
) -> dict:
    if not server_chat_enabled():
        raise HTTPException(status_code=404, detail="Server chat storage is not enabled.")
    if user_id in ("anonymous", "shared"):
        raise HTTPException(status_code=400, detail="Chat sync requires a personal account.")
    save_chat_state(
        user_id,
        {"threads": body.threads, "active_id": body.active_id},
    )
    s = get_settings()
    if r2_sync_configured(s):
        # Force backup so redeploys don't lose recent chats (throttled sync can skip).
        background_tasks.add_task(schedule_users_db_sync, s, True)
    return {"ok": True}


@app.post("/api/admin/sync-users-db")
def admin_sync_users_db(request: Request) -> dict:
    """One-shot backup of users.db to R2 (header X-Sync-Secret)."""
    s = get_settings()
    secret = sync_secret(s)
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    if request.headers.get("X-Sync-Secret", "") != secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not r2_sync_configured(s):
        raise HTTPException(status_code=503, detail="R2 sync is not configured")
    if not sync_users_db_to_r2(s, force=True):
        raise HTTPException(status_code=500, detail="Sync failed")
    return {"ok": True}


def _persist_usage(user_id: str, kind: str, usage: dict, settings) -> None:
    if not usage:
        return
    try:
        record_usage_event(
            user_id=user_id,
            kind=kind,
            model=str(usage.get("model") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0) or None,
            estimated=bool(usage.get("estimated")),
            settings=settings,
        )
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/admin/overview")
def api_admin_overview(_admin: CurrentAdminUser) -> dict:  # noqa: ARG001
    return admin_overview(get_settings())


@app.get("/api/admin/users")
def api_admin_users(_admin: CurrentAdminUser) -> dict:  # noqa: ARG001
    return {"users": admin_list_users(get_settings())}


@app.get("/api/admin/users/{user_id}")
def api_admin_user_detail(user_id: str, _admin: CurrentAdminUser) -> dict:  # noqa: ARG001
    detail = admin_user_detail(user_id, get_settings())
    if not detail:
        raise HTTPException(status_code=404, detail="用户不存在")
    return detail


@app.get("/api/admin/users/{user_id}/usage")
def api_admin_user_usage(user_id: str, _admin: CurrentAdminUser, days: int = 30) -> dict:  # noqa: ARG001
    data = admin_user_usage(user_id, get_settings(), days=max(1, min(days, 90)))
    if not data:
        raise HTTPException(status_code=404, detail="用户不存在")
    return data


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    out: dict = {
        "status": "ok",
        "chat_model": effective_chat_model(s),
        "chat_provider": "kimi" if uses_kimi_chat(s) else "ai_builders",
        "embedding_model": s.ai_embedding_model,
        "users_db_url_set": bool(s.users_db_url.strip()),
        "backup_r2_configured": r2_sync_configured(s),
    }
    if not s.public_deploy:
        out["vault"] = str(s.vault_path)
    return out


@app.get("/api/index/status")
def index_status(user_id: CurrentUserId) -> dict:  # noqa: ARG001
    s = get_settings()
    meta = read_index_meta(s.data_dir.resolve())
    if s.public_deploy:
        return {
            "ready": bool(meta.get("ready")),
            "chunk_count": meta.get("chunk_count", 0),
            "vector_enabled": bool(meta.get("vector_enabled")),
            "last_indexed_at": meta.get("last_indexed_at"),
        }
    return meta


@app.post("/api/index")
async def run_index(user_id: CurrentUserId) -> dict:  # noqa: ARG001
    s = get_settings()
    if s.public_deploy:
        raise HTTPException(status_code=403, detail="Indexing is disabled on the public deployment.")
    try:
        return await rebuild_index_async(s)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _ndjson_chat_events(
    body: ChatBody,
    request: Request,
    user_id: str,
) -> AsyncIterator[bytes]:
    s = get_settings()
    yield (json.dumps({"meta": {"status": "started"}}, ensure_ascii=False) + "\n").encode()

    async def _cancelled() -> bool:
        return await request.is_disconnected()

    user_text = body.message.strip()
    history = [{"role": m.role, "content": m.content} for m in body.history]
    images = [{"mime": i.mime, "data_base64": i.data_base64} for i in body.images]
    context_summary = body.context_summary.strip()
    questions_guide = load_questions_guide()
    account_user = user_id not in ("anonymous", "shared")
    user_memory = load_user_memory(user_id, s) if account_user else ""

    if account_user:
        quota = get_token_quota(user_id, s)
        if not quota.get("allowed", True):
            yield (
                json.dumps(
                    {"error": quota.get("message") or TOKEN_QUOTA_EXCEEDED_MESSAGE},
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
            return

    if await _cancelled():
        return

    phase_usage: dict = {}
    try:
        decided = await _await_unless_disconnected(
            request,
            decide_phase(
                settings=s,
                user_message=user_text or "（用户上传了聊天截图）",
                history=history,
                context_summary=context_summary,
                has_images=bool(images),
                questions_guide=questions_guide,
            ),
        )
        if decided is None:
            return
        phase, phase_usage = decided
    except Exception:  # noqa: BLE001
        phase = "clarify" if len(history) < 2 else "advise"
        phase_usage = {}

    if account_user and phase_usage:
        _persist_usage(user_id, "phase", phase_usage, s)

    if await _cancelled():
        return

    yield (
        json.dumps({"meta": {"phase": phase, "status": "phase_decided"}}, ensure_ascii=False)
        + "\n"
    ).encode()

    chunks = []
    routing_info: dict = {"rag_used": False, "phase": phase}
    if phase == "advise":
        data_dir = s.data_dir.resolve()
        meta = read_index_meta(data_dir)
        retrieve_q = user_text or context_summary or "亲密关系建议"
        try:
            retrieved = await _await_unless_disconnected(
                request,
                retrieve_context(
                    retrieve_q,
                    settings=s,
                    meta=meta,
                ),
            )
            if retrieved is None:
                return
            chunks, routing_info = retrieved
        except RuntimeError as e:
            yield (json.dumps({"error": str(e)}, ensure_ascii=False) + "\n").encode()
            return
        except Exception as e:  # noqa: BLE001
            yield (json.dumps({"error": f"检索失败：{e!s}"}, ensure_ascii=False) + "\n").encode()
            return
    elif phase == "out_of_scope":
        routing_info = {
            "phase": "out_of_scope",
            "rag_used": False,
            "skipped_retrieval": True,
        }
    else:
        routing_info = {
            "phase": "clarify",
            "rag_used": False,
            "skipped_retrieval": True,
        }

    if await _cancelled():
        return

    messages, rag_used = build_consult_chat_messages(
        phase=phase,
        user_text=user_text,
        history=history,
        context_summary=context_summary,
        chunks=chunks,
        images=images,
        questions_guide=questions_guide,
        settings=s,
        user_memory=user_memory,
    )
    routing_info["rag_used"] = rag_used
    routing_info["phase"] = phase
    routing_info["user_memory_used"] = bool(user_memory)
    relevant_chunks = filter_relevant_chunks(chunks, s) if rag_used else []

    if s.public_deploy:
        yield (
            json.dumps(
                {
                    "meta": {
                        "public_deploy": True,
                        "rag_used": rag_used,
                        "phase": phase,
                        "user_memory_used": bool(user_memory),
                    }
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode()
    else:
        ctx_lines = [
            {
                "id": c.id,
                "note_path": c.note_path,
                "note_title": c.note_title,
                "heading_path": c.heading_path,
                "source": c.source,
            }
            for c in relevant_chunks
        ]
        payload = {"sources": ctx_lines, "routing": routing_info}
        yield (json.dumps(payload, ensure_ascii=False) + "\n").encode()

    if await _cancelled():
        return

    assistant_acc = []
    chat_usage: dict = {}
    async for line in stream_chat_completion(s, messages, should_stop=_cancelled):
        if await _cancelled():
            return
        try:
            obj = json.loads(line)
            if isinstance(obj.get("text"), str):
                assistant_acc.append(obj["text"])
            if isinstance(obj.get("usage"), dict):
                chat_usage = obj["usage"]
                # Do not forward usage blobs to the browser.
                continue
        except json.JSONDecodeError:
            pass
        yield line.encode()

    if account_user:
        if chat_usage:
            _persist_usage(user_id, "chat", chat_usage, s)
        elif assistant_acc:
            approx_out = max(1, len("".join(assistant_acc)) // 2)
            approx_in = max(1, (len(user_text) + len(context_summary) + len(user_memory)) // 2)
            _persist_usage(
                user_id,
                "chat",
                {
                    "model": effective_chat_model(s),
                    "prompt_tokens": approx_in,
                    "completion_tokens": approx_out,
                    "total_tokens": approx_in + approx_out,
                    "estimated": True,
                },
                s,
            )

    if await _cancelled():
        return

    assistant_text = "".join(assistant_acc).strip()
    should_summarize = (
        bool(assistant_text)
        and phase != "out_of_scope"
        and (len(history) + 1 >= 4 or len((context_summary or "")) > 0 or phase == "advise")
    )
    new_summary = context_summary
    if should_summarize:
        try:
            summarized = await _await_unless_disconnected(
                request,
                refresh_context_summary(
                    settings=s,
                    previous_summary=context_summary,
                    history=history,
                    latest_user=user_text or "（截图）",
                    latest_assistant=assistant_text,
                ),
            )
            if summarized is None:
                return
            new_summary, summary_usage = summarized
            if account_user and summary_usage:
                _persist_usage(user_id, "summary", summary_usage, s)
        except Exception:  # noqa: BLE001
            new_summary = context_summary

    if new_summary and new_summary != context_summary:
        yield (
            json.dumps(
                {"meta": {"context_summary": new_summary, "phase": phase}},
                ensure_ascii=False,
            )
            + "\n"
        ).encode()

    # Cross-thread long-term memory (account users only).
    if account_user and should_update_user_memory(
        phase=phase,
        user_text=user_text,
        assistant_text=assistant_text,
    ):
        try:
            merged = await _await_unless_disconnected(
                request,
                refresh_user_memory(
                    settings=s,
                    previous_memory=user_memory,
                    latest_user=user_text or "（截图）",
                    latest_assistant=assistant_text,
                    thread_summary=new_summary or context_summary,
                    phase=phase,
                ),
            )
            if merged is None:
                return
            memory_text, memory_usage = merged
            if account_user and memory_usage:
                _persist_usage(user_id, "memory", memory_usage, s)
            if memory_text.strip() and memory_text.strip() != user_memory.strip():
                save_user_memory(user_id, memory_text, s)
                if r2_sync_configured(s):
                    schedule_users_db_sync(s, True)
                yield (
                    json.dumps(
                        {"meta": {"user_memory_updated": True, "phase": phase}},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode()
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/user/memory")
def get_user_memory(user_id: CurrentUserId) -> dict:
    if user_id in ("anonymous", "shared"):
        raise HTTPException(status_code=400, detail="需要登录账户才能使用长时记忆。")
    text = load_user_memory(user_id)
    return {"memory": text, "updated": bool(text)}


@app.delete("/api/user/memory")
def delete_user_memory(user_id: CurrentUserId, background_tasks: BackgroundTasks) -> dict:
    if user_id in ("anonymous", "shared"):
        raise HTTPException(status_code=400, detail="需要登录账户才能使用长时记忆。")
    clear_user_memory(user_id)
    s = get_settings()
    if r2_sync_configured(s):
        background_tasks.add_task(schedule_users_db_sync, s, True)
    return {"ok": True, "memory": ""}


T = TypeVar("T")


async def _await_unless_disconnected(request: Request, coro: Awaitable[T]) -> T | None:
    """Run coroutine but cancel it if the client disconnects."""
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return None
            done, _pending = await asyncio.wait({task}, timeout=0.35)
            if done:
                break
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request, user_id: CurrentUserId) -> StreamingResponse:
    return StreamingResponse(
        _ndjson_chat_events(body, request, user_id),
        media_type="application/x-ndjson",
    )


def _admin_index() -> FileResponse:
    index = _web_dist() / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Frontend not built.")
    return FileResponse(index)


@app.get("/admin")
@app.get("/admin/")
def admin_spa() -> FileResponse:
    """Serve SPA shell for the ops dashboard (refresh-safe)."""
    return _admin_index()


@app.get("/admin/{path:path}")
def admin_spa_nested(path: str) -> FileResponse:  # noqa: ARG001
    return _admin_index()


_dist = _web_dist()
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="static")
