from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.http_client import async_gateway_client, format_gateway_connect_error
from app.retrieve import RetrievedChunk
from app.settings import Settings, get_settings


def filter_relevant_chunks(
    chunks: list[RetrievedChunk],
    settings: Settings | None = None,
) -> list[RetrievedChunk]:
    settings = settings or get_settings()
    if not chunks:
        return []
    floor = settings.rag_min_fused_score
    if floor <= 0:
        return list(chunks)
    return [c for c in chunks if c.score >= floor]


def chunks_are_relevant(
    chunks: list[RetrievedChunk],
    settings: Settings | None = None,
) -> bool:
    return len(filter_relevant_chunks(chunks, settings)) > 0


def build_system_prompt(
    settings: Settings | None = None,
    *,
    use_notes: bool = True,
) -> str:
    settings = settings or get_settings()
    if use_notes:
        base = (
            "You are the user's 「Romance Expert」 assistant for intimate relationships (恋爱、婚姻、择偶、亲密关系等). "
            "When the provided note excerpts clearly address the question, ground your answer in them. "
            "When excerpts are only weakly related or do not cover the question, still answer helpfully from your "
            "general expertise—do NOT refuse, and do NOT say the notes lack information or that you have nothing relevant. "
            "The excerpts may come from several DIFFERENT notes: "
            "synthesize complementary points across them when they all relate to the question. "
            "Be concise, warm, and practical. "
            "Do not invent personal facts about the user that are not in the excerpts. "
            "This is reflection and relationship guidance—not medical/legal advice."
        )
    else:
        base = (
            "You are the user's 「Romance Expert」 assistant for intimate relationships (恋爱、婚姻、择偶、亲密关系等). "
            "The personal note library did not return clearly relevant excerpts for this question. "
            "Answer helpfully using your general expertise. "
            "Be concise, warm, and practical. "
            "Do not mention missing notes, an empty knowledge base, retrieval, or internal documents. "
            "This is general relationship guidance—not medical/legal advice."
        )
    if settings.public_deploy:
        return (
            base
            + " "
            "Write as a direct answer only: do NOT include citation markers like [1] or [2], "
            "do NOT mention excerpt numbers, note titles, file paths, or phrases like 「笔记」「摘录」「来源」. "
            "Integrate any note material invisibly—the user should see only your advice."
        )
    if use_notes:
        return (
            base
            + " "
            "Citations: put ONLY bracketed numbers [1], [2], … immediately after the sentence or clause they support—matching the excerpt numbers in the prompt. "
            "Do NOT use phrases like 「笔记1」「摘录9」「从笔记X」「根据第几条」or any wording that verbally labels sources by excerpt index; integrate the substance in your own words and cite with [n] alone."
        )
    return base


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


def build_chat_messages(
    question: str,
    chunks: list[RetrievedChunk],
    settings: Settings | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """Build OpenAI-style messages; returns (messages, rag_used)."""
    settings = settings or get_settings()
    relevant = filter_relevant_chunks(chunks, settings)
    use_notes = len(relevant) > 0
    if use_notes:
        user_content = build_user_message(question, relevant)
    else:
        user_content = question.strip()
    messages = [
        {"role": "system", "content": build_system_prompt(settings, use_notes=use_notes)},
        {"role": "user", "content": user_content},
    ]
    return messages, use_notes


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
