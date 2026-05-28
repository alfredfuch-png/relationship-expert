from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import (
    auth_enabled,
    auth_mode,
    authenticate_login,
    clear_session_cookie,
    CurrentUserId,
    resolve_user,
    server_chat_enabled,
    set_session_cookie,
)
from app.chat import build_system_prompt, build_user_message, stream_chat_completion
from app.indexing import read_index_meta, rebuild_index_async
from app.retrieve import retrieve_context
from app.settings import _project_root, get_settings
from app.startup import prepare_runtime_data
from app.users_store import load_chat_state, save_chat_state


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


app = FastAPI(title="Romance Expert RAG", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class LoginBody(BaseModel):
    username: str = Field(default="", max_length=64)
    password: str = Field(min_length=1, max_length=256)


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
        "server_chat": server_chat_enabled(s),
    }


@app.post("/api/auth/login")
def auth_login(body: LoginBody, response: Response) -> dict:
    s = get_settings()
    mode = auth_mode(s)
    if not auth_enabled(s):
        return {"ok": True, "auth_required": False, "auth_mode": "none"}
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
        "server_chat": server_chat_enabled(s),
    }


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


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
def put_chat_state(body: ChatStateBody, user_id: CurrentUserId) -> dict:
    if not server_chat_enabled():
        raise HTTPException(status_code=404, detail="Server chat storage is not enabled.")
    if user_id in ("anonymous", "shared"):
        raise HTTPException(status_code=400, detail="Chat sync requires a personal account.")
    save_chat_state(
        user_id,
        {"threads": body.threads, "active_id": body.active_id},
    )
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, str]:
    s = get_settings()
    out: dict[str, str] = {
        "status": "ok",
        "chat_model": s.ai_chat_model,
        "embedding_model": s.ai_embedding_model,
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


async def _ndjson_chat_events(message: str) -> AsyncIterator[bytes]:
    s = get_settings()
    data_dir = s.data_dir.resolve()
    meta = read_index_meta(data_dir)

    try:
        chunks, routing_info = await retrieve_context(
            message,
            settings=s,
            meta=meta,
        )
    except RuntimeError as e:
        yield (json.dumps({"error": str(e)}) + "\n").encode()
        return

    if s.public_deploy:
        yield (json.dumps({"meta": {"public_deploy": True}}, ensure_ascii=False) + "\n").encode()
    else:
        ctx_lines = [
            {
                "id": c.id,
                "note_path": c.note_path,
                "note_title": c.note_title,
                "heading_path": c.heading_path,
                "source": c.source,
            }
            for c in chunks
        ]
        payload = {"sources": ctx_lines, "routing": routing_info}
        yield (json.dumps(payload, ensure_ascii=False) + "\n").encode()

    user_content = build_user_message(message, chunks)
    messages = [
        {"role": "system", "content": build_system_prompt(s)},
        {"role": "user", "content": user_content},
    ]

    async for line in stream_chat_completion(s, messages):
        yield line.encode()


@app.post("/api/chat")
async def chat(body: ChatBody, user_id: CurrentUserId) -> StreamingResponse:  # noqa: ARG001
    return StreamingResponse(
        _ndjson_chat_events(body.message),
        media_type="application/x-ndjson",
    )


_dist = _web_dist()
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="static")
