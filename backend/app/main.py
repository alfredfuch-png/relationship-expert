from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat import build_system_prompt, build_user_message, stream_chat_completion
from app.indexing import read_index_meta, rebuild_index_async
from app.retrieve import retrieve_context
from app.settings import get_settings

app = FastAPI(title="Romance Expert RAG", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@app.get("/api/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {
        "status": "ok",
        "vault": str(s.vault_path),
        "chat_model": s.ai_chat_model,
        "embedding_model": s.ai_embedding_model,
    }


@app.get("/api/index/status")
def index_status() -> dict:
    s = get_settings()
    meta = read_index_meta(s.data_dir.resolve())
    return meta


@app.post("/api/index")
async def run_index() -> dict:
    s = get_settings()
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
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": user_content},
    ]

    async for line in stream_chat_completion(s, messages):
        yield line.encode()


@app.post("/api/chat")
async def chat(body: ChatBody) -> StreamingResponse:
    return StreamingResponse(
        _ndjson_chat_events(body.message),
        media_type="application/x-ndjson",
    )
