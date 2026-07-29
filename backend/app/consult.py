from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import httpx

from app.http_client import async_gateway_client
from app.settings import Settings, get_settings, _project_root

Phase = Literal["clarify", "advise"]

SKIP_ADVISE_PATTERNS = (
    "直接说",
    "不用问",
    "别问了",
    "直接给建议",
    "直接回答",
    "跳过追问",
    "给我答案",
)


def load_questions_guide() -> str:
    """Load Questions.md from project root (or backend sibling)."""
    candidates = [
        _project_root() / "Questions.md",
        Path(__file__).resolve().parents[2] / "Questions.md",
        Path("/app/Questions.md"),
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return (
        "每次回复追问不超过 3 个问题。信息明显不足时先追问，够了再给建议。"
        "用户说直接说/不用问了则跳过追问。"
    )


def wants_skip_clarify(text: str) -> bool:
    t = (text or "").strip()
    return any(p in t for p in SKIP_ADVISE_PATTERNS)


def build_consult_system_prompt(
    *,
    phase: Phase,
    use_notes: bool,
    public_deploy: bool,
    questions_guide: str,
) -> str:
    guide = questions_guide.strip()
    common = (
        "你是「阿FU」，用户的亲密关系顾问（恋爱、婚姻、择偶、相处、矛盾等）。"
        "语气温暖、务实，像真人顾问微信聊天，不要写成报告或小论文。"
        "这是咨询与反思支持，不是医疗/法律建议。"
        "不要编造用户没说过的个人事实。"
        "\n\n【排版格式（必须遵守）】\n"
        "- 不要使用 Markdown：禁止 **加粗**、*斜体*、# 标题、```代码块、[链接](url) 等标记。\n"
        "- 用换行分段；列举用「1.」「2.」或「一、二、」即可。\n"
        "- 需要强调时用中文自然说法（如「重点是」「建议你先…」），不要用星号包文字。\n"
        f"\n【追问与建议规则（必须遵守）】\n{guide}\n"
        "额外硬性约束：\n"
        "- 若本轮是追问阶段：只补关键缺口，每次回复里问句不超过 3 个；可鼓励用户上传聊天截图或粘贴关键对话原文。\n"
        "- 若本轮是建议阶段：给出分段、可执行的建议；篇幅可稍长，但结构清晰。\n"
        "- 用户说「直接说 / 不用问了」等时，必须进入建议，不再追问。\n"
    )
    if phase == "clarify":
        common += (
            "\n【当前阶段：追问】信息仍明显不足。不要给完整长建议；"
            "先简短回应理解，再提出不超过 3 个关键问题。"
        )
    else:
        common += (
            "\n【当前阶段：正式建议】背景已够或用户要求直接建议。"
            "请给出有针对性的分段建议；若有笔记摘录则优先结合摘录，但不要说「没有相关信息」。"
        )
        if use_notes:
            common += " 摘录相关时自然融入建议。"
        if public_deploy:
            common += (
                " 不要输出 [1]/[2] 引用编号，不要提笔记/摘录/来源等字样。"
            )
        elif use_notes:
            common += (
                " 引用只用 [n] 编号紧跟句子，不要用「笔记1」等说法。"
            )
    return common


async def _chat_json(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 400,
) -> str:
    from app.chat import effective_chat_model, uses_kimi_chat

    if uses_kimi_chat(settings):
        token = settings.kimi_api_key.strip()
        base = settings.kimi_api_base_url.rstrip("/") or "https://api.moonshot.cn/v1"
        model = effective_chat_model(settings)
        extra: dict[str, Any] = {"temperature": 1.0, "reasoning_effort": "low"}
    else:
        token = settings.ai_builder_token
        base = settings.ai_api_base_url.rstrip("/")
        model = settings.ai_chat_model
        extra = {"temperature": 0.1}
    if not token:
        return ""

    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        **extra,
    }
    async with async_gateway_client(settings, timeout_seconds=60.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return str(content).strip()


async def decide_phase(
    *,
    settings: Settings,
    user_message: str,
    history: list[dict[str, str]],
    context_summary: str,
    has_images: bool,
    questions_guide: str,
) -> Phase:
    if wants_skip_clarify(user_message):
        return "advise"

    # Enough back-and-forth and summary already present → lean advise
    user_turns = sum(1 for m in history if m.get("role") == "user")
    if context_summary.strip() and user_turns >= 3 and not _looks_sparse(user_message):
        # Still ask model if latest message introduces a new topic needing clarify
        pass

    hist_snip = []
    for m in history[-8:]:
        role = m.get("role", "")
        content = (m.get("content") or "")[:400]
        hist_snip.append(f"{role}: {content}")
    hist_text = "\n".join(hist_snip) if hist_snip else "(无)"
    summary = context_summary.strip() or "(无)"

    prompt = (
        "根据咨询规则判断下一轮应处于哪一阶段。只输出 JSON："
        '{"phase":"clarify"} 或 {"phase":"advise"}。\n'
        "clarify=明显缺关键背景，应先追问；advise=信息已够或应给建议。\n"
        f"规则摘要：\n{questions_guide[:3500]}\n\n"
        f"上下文摘要：\n{summary}\n\n"
        f"最近对话：\n{hist_text}\n\n"
        f"用户本轮：{user_message[:1500]}\n"
        f"本轮是否附带截图：{'是' if has_images else '否'}\n"
    )
    raw = await _chat_json(
        settings,
        [
            {
                "role": "system",
                "content": "你是阶段分类器。只输出一个 JSON 对象，不要其他文字。",
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=80,
    )
    phase = _parse_phase_json(raw)
    if phase:
        return phase

    # Heuristic fallback
    if user_turns == 0 and not context_summary.strip() and len(user_message.strip()) < 80:
        return "clarify"
    if user_turns >= 4 or (context_summary.strip() and user_turns >= 2):
        return "advise"
    return "clarify"


def _looks_sparse(text: str) -> bool:
    return len((text or "").strip()) < 40


def _parse_phase_json(raw: str) -> Phase | None:
    if not raw:
        return None
    m = re.search(r"\{[^{}]*\}", raw)
    if not m:
        if "advise" in raw.lower() and "clarify" not in raw.lower():
            return "advise"
        if "clarify" in raw.lower():
            return "clarify"
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    phase = str(obj.get("phase", "")).strip().lower()
    if phase in ("clarify", "advise"):
        return phase  # type: ignore[return-value]
    return None


async def refresh_context_summary(
    *,
    settings: Settings,
    previous_summary: str,
    history: list[dict[str, str]],
    latest_user: str,
    latest_assistant: str,
) -> str:
    """Compress conversation into a structured Chinese summary."""
    hist_lines = []
    for m in history[-12:]:
        hist_lines.append(f"{m.get('role')}: {(m.get('content') or '')[:500]}")
    prompt = (
        "请把亲密关系咨询对话压缩成结构化中文摘要，便于后续继续咨询。"
        "保留：问题类型、用户目标、对方情况、关系阶段、关键事实、用户情绪、已给建议要点、仍缺信息。"
        "不要空话，不要编造。若某项未知写「未提供」。直接输出摘要正文。\n\n"
        f"旧摘要：\n{previous_summary.strip() or '(无)'}\n\n"
        f"近期对话：\n" + "\n".join(hist_lines) + "\n\n"
        f"最新用户：{latest_user[:1200]}\n"
        f"最新助手：{latest_assistant[:2000]}\n"
    )
    out = await _chat_json(
        settings,
        [
            {"role": "system", "content": "你是咨询上下文压缩助手。只输出摘要。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=600,
    )
    return out or previous_summary.strip()


def build_history_messages(
    history: list[dict[str, str]],
    *,
    limit: int = 10,
) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:6000]})
    return cleaned[-limit:]


def build_multimodal_user_content(
    text: str,
    images: list[dict[str, str]],
) -> str | list[dict[str, Any]]:
    """OpenAI/Kimi vision content parts. images: {mime, data_base64}."""
    text = (text or "").strip()
    parts: list[dict[str, Any]] = []
    for img in images[:3]:
        mime = (img.get("mime") or "image/jpeg").strip()
        b64 = (img.get("data_base64") or "").strip()
        if not b64:
            continue
        if b64.startswith("data:"):
            url = b64
        else:
            url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if text:
        parts.append({"type": "text", "text": text})
    elif parts:
        parts.append(
            {
                "type": "text",
                "text": "请结合以上聊天截图，理解对话内容，并按咨询规则回应。",
            }
        )
    if not parts:
        return text or "（空消息）"
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0].get("text") or text)
    return parts
