from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
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


def build_consult_chat_messages(
    *,
    phase: str,
    user_text: str,
    history: list[dict[str, str]],
    context_summary: str,
    chunks: list[RetrievedChunk],
    images: list[dict[str, str]],
    questions_guide: str,
    settings: Settings | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Build messages for clarify/advise consult flow. Returns (messages, rag_used)."""
    from app.consult import (
        Phase,
        build_consult_system_prompt,
        build_history_messages,
        build_multimodal_user_content,
    )

    settings = settings or get_settings()
    phase_t: Phase = "advise" if phase == "advise" else "clarify"
    relevant = filter_relevant_chunks(chunks, settings) if phase_t == "advise" else []
    use_notes = phase_t == "advise" and len(relevant) > 0

    system = build_consult_system_prompt(
        phase=phase_t,
        use_notes=use_notes,
        public_deploy=settings.public_deploy,
        questions_guide=questions_guide,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    summary = (context_summary or "").strip()
    if summary:
        messages.append(
            {
                "role": "system",
                "content": f"【已压缩的咨询上下文】\n{summary}",
            }
        )

    for h in build_history_messages(history, limit=10):
        messages.append({"role": h["role"], "content": h["content"]})

    if use_notes:
        notes = build_user_message(user_text or "（见截图/上文）", relevant)
        # When images present, send multimodal: notes text + images
        if images:
            content: Any = [
                {"type": "text", "text": notes},
            ]
            multi = build_multimodal_user_content("", images)
            if isinstance(multi, list):
                content.extend(p for p in multi if p.get("type") == "image_url")
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": notes})
    else:
        messages.append(
            {
                "role": "user",
                "content": build_multimodal_user_content(user_text, images),
            }
        )

    return messages, use_notes


def effective_chat_model(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.kimi_api_key.strip():
        return (settings.kimi_chat_model or "kimi-k3").strip()
    return settings.ai_chat_model.strip() or "deepseek"


def uses_kimi_chat(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.kimi_api_key.strip())


async def stream_chat_completion(
    settings: Settings,
    messages: list[dict[str, Any]],
    should_stop: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    if uses_kimi_chat(settings):
        token = settings.kimi_api_key.strip()
        base = settings.kimi_api_base_url.rstrip("/") or "https://api.moonshot.cn/v1"
        model = effective_chat_model(settings)
        host_hint = "Kimi（api.moonshot.cn）"
    else:
        if not settings.ai_builder_token:
            yield json.dumps(
                {"error": "未配置聊天模型：请在 .env 设置 KIMI_API_KEY，或设置 AI_BUILDER_TOKEN。"}
            ) + "\n"
            return
        token = settings.ai_builder_token
        base = settings.ai_api_base_url.rstrip("/")
        model = settings.ai_chat_model
        host_hint = "AI Builders（space.ai-builders.com）"

    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    # kimi-k3: always thinking; temperature fixed at 1; prefer faster replies for chat UX
    if uses_kimi_chat(settings):
        payload["temperature"] = 1.0
        payload["reasoning_effort"] = "low"
    else:
        payload["temperature"] = 0.25

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
                    if should_stop and await should_stop():
                        await resp.aclose()
                        return
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
                            # Kimi may return content as string or list of parts
                            if isinstance(piece, list):
                                piece = "".join(
                                    p.get("text", "") if isinstance(p, dict) else str(p)
                                    for p in piece
                                )
                            if piece:
                                yield json.dumps({"text": piece}) + "\n"
        except httpx.ConnectError as e:
            yield json.dumps(
                {"error": format_gateway_connect_error(e, host_hint=host_hint)},
                ensure_ascii=False,
            ) + "\n"
        except httpx.HTTPError as e:
            yield json.dumps({"error": f"请求聊天 API 失败：{e!s}"}, ensure_ascii=False) + "\n"
