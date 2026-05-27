from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.http_client import async_gateway_client, format_gateway_connect_error
from app.retrieve import RetrievedChunk
from app.settings import Settings, get_settings


def build_system_prompt(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    base = (
        "You are the user's 「Romance Expert」 assistant for intimate relationships (恋爱、婚姻、择偶、亲密关系等). "
        "Answer using ONLY the provided note excerpts when they are relevant. "
        "If excerpts are insufficient, say so briefly without naming internal documents. "
        "The excerpts may come from several DIFFERENT notes: "
        "synthesize complementary points across them when they all relate to the question; "
        "do not answer from only the first or highest-listed excerpt unless the others truly add nothing. "
        "Be concise, warm, and practical. "
        "Do not invent facts not supported by excerpts. This is reflection and note-based guidance—not medical/legal advice."
    )
    if settings.public_deploy:
        return (
            base
            + " "
            "Write as a direct answer only: do NOT include citation markers like [1] or [2], "
            "do NOT mention excerpt numbers, note titles, file paths, or phrases like 「笔记」「摘录」「来源」. "
            "Integrate the material invisibly—the user should see only your advice."
        )
    return (
        base
        + " "
        "Citations: put ONLY bracketed numbers [1], [2], … immediately after the sentence or clause they support—matching the excerpt numbers in the prompt. "
        "Do NOT use phrases like 「笔记1」「摘录9」「从笔记X」「根据第几条」or any wording that verbally labels sources by excerpt index; integrate the substance in your own words and cite with [n] alone."
    )


def build_user_message(question: str, chunks: list[RetrievedChunk]) -> str:
    lines: list[str] = []
    for i, c in enumerate(chunks, start=1):
        lines.append(
            f"[{i}] 《{c.note_title}》 ({c.note_path}) — {c.heading_path}\n{c.text}\n"
        )
    body = "\n".join(lines)
    return (
        "Excerpts from the user's Obsidian notes (folder: 关于亲密关系 / intimate relationships):\n\n"
        f"{body}\n\nQuestion:\n{question}"
    )


async def stream_chat_completion(
    settings: Settings,
    messages: list[dict[str, str]],
) -> AsyncIterator[str]:
    if not settings.ai_builder_token:
        yield json.dumps({"error": "AI_BUILDER_TOKEN is not set in .env"}) + "\n"
        return

    base = settings.ai_api_base_url.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.ai_builder_token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": settings.ai_chat_model,
        "messages": messages,
        "temperature": 0.25,
        "stream": True,
    }

    async with async_gateway_client(settings, timeout_seconds=180.0) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    yield json.dumps(
                        {
                            "error": (
                                f"Chat API HTTP {resp.status_code}: "
                                f"{detail.decode(errors='replace')[:500]}"
                            )
                        }
                    ) + "\n"
                    return

                buf = ""
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    buf += chunk.decode(errors="ignore")
                    while True:
                        line_end = buf.find("\n")
                        if line_end < 0:
                            break
                        line = buf[:line_end].strip()
                        buf = buf[line_end + 1 :]
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices")
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield json.dumps({"text": piece}) + "\n"
        except httpx.ConnectError as e:
            yield json.dumps({"error": format_gateway_connect_error(e)}, ensure_ascii=False) + "\n"
        except httpx.HTTPError as e:
            yield json.dumps({"error": f"请求 AI 网关失败：{e!s}"}, ensure_ascii=False) + "\n"
