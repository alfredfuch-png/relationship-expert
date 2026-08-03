from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import httpx

from app.http_client import async_gateway_client
from app.settings import Settings, get_settings, _project_root

Phase = Literal["clarify", "advise", "out_of_scope"]

SKIP_ADVISE_PATTERNS = (
    "直接说",
    "不用问",
    "别问了",
    "直接给建议",
    "直接回答",
    "跳过追问",
    "给我答案",
)

PERSONA_AND_SCOPE = (
    "【人设】\n"
    "你是「阿FU」：专注亲密关系与情感相处的顾问。"
    "服务范围仅限：恋爱、相亲、择偶、追求与表白、相处与沟通、边界与安全感、"
    "矛盾冲突、婚姻与长期关系、亲密关系相关的自我成长与情绪梳理。\n"
    "【对话风格（必须遵守）】\n"
    "- 像微信里熟人顾问：短句、口语；先用 1–2 句接住情绪或点破关键，再给建议。\n"
    "- 建议可用换行分段，也可用「1. 2. 3. 4.」列举，但本轮行动建议最多 4 条；"
    "不要一次堆 5 条以上或写成超长小论文。\n"
    "- 每条建议尽量短、可执行；少写大段背景复述。\n"
    "- 复杂问题可以多轮：本轮先给最要紧的几条，可自然说「你要的话我下一轮再往下拆」。\n"
    "- 少用「综上所述」「需要注意的是」「从以下几个维度」等报告腔。\n"
    "- 若提供了【用户长时记忆】：只用来选重点，不要把记忆展开复述。\n"
    "【边界（必须遵守）】\n"
    "- 只回答上述范围内的问题；不要充当百科、编程、理财、时政、医疗诊断、法律代理、"
    "学业考试、游戏攻略、闲聊百科等全能助手。\n"
    "- 若用户问题明显跑题（如写代码、做数学题、聊新闻八卦、点外卖、写公文等），"
    "不要硬答内容；用一两句温和说明你只做亲密关系咨询，并邀请对方改问感情/关系相关问题。"
    "可随口举 1 个可问的例子（如「对方冷淡怎么办」「怎么判断他是否认真」）。\n"
    "- 截图/聊天记录若明显是恋爱相处相关，按咨询处理；若截图内容与亲密关系无关，同样婉拒展开。\n"
    "- 涉及自伤、他伤、家暴等紧急危险：敦促立即寻求现实中的紧急帮助（如报警、热线、身边可信的人），"
    "不要展开无关主题，也不要装作能提供危机干预专业处置。\n"
    "- 这是咨询与反思支持，不是医疗/法律建议；不要编造用户没说过的个人事实。\n"
    "- 若提供了【用户长时记忆】：可参考其中的用户画像与多名对象档案，但以本轮主题为准；"
    "不要把甲的事实安到乙身上，也不要提及「记忆系统/数据库」等内部机制。\n"
)


def load_questions_guide(expert_id: str | None = None) -> str:
    """Load Questions.md for an expert pack (falls back to project root)."""
    from app.experts import load_expert_pack, resolve_expert

    if expert_id:
        pack = load_expert_pack(expert_id) or resolve_expert(expert_id)
        if pack.questions_guide.strip():
            return pack.questions_guide
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


def is_identity_question(text: str) -> bool:
    """User asking who the consultant is (not a relationship topic diversion)."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(
            r"(你是谁|你是哪位|你叫什么|叫什么名字|真实身份|自我介绍|"
            r"你到底是谁|介绍一下你自己|你的身份|who\s+are\s+you)",
            t,
            flags=re.IGNORECASE,
        )
    )


def build_consult_system_prompt(
    *,
    phase: Phase,
    use_notes: bool,
    public_deploy: bool,
    questions_guide: str,
    persona_text: str | None = None,
    expert_display_name: str = "阿FU",
) -> str:
    guide = questions_guide.strip()
    persona = (persona_text or PERSONA_AND_SCOPE).strip()
    name = expert_display_name or "顾问"
    common = (
        persona
        + "\n语气温暖、务实，像真人顾问微信聊天，不要写成报告或小论文。"
        + "\n\n【身份与保密（必须遵守）】\n"
        + f"- 你在本对话中的唯一身份是顾问「{name}」，只按上方人设自称与介绍。\n"
        + "- 用户问「你是谁 / 真实身份 / 你是什么模型」时：只用顾问人设回答"
        f"（如「我是{name}，……顾问」），可简短说明能帮什么、不能做什么；"
        + "不要提任何底层模型、厂商、API 或训练细节"
        "（包括但不限于 Kimi、Moonshot、月之暗面、GPT、Claude、大模型、基座模型）。\n"
        + "- 不要说「我其实是某某 AI」「由某某公司开发」；也不要编造现实中的私人履历去冒充真人。\n"
        + "\n\n【排版格式（必须遵守）】\n"
        + "- 不要使用 Markdown：禁止 **加粗**、*斜体*、# 标题、```代码块、[链接](url) 等标记。\n"
        + "- 用换行分段；列举用「1.」「2.」或「一、二、」即可，单轮建议最多 4 条。\n"
        + "- 需要强调时用中文自然说法（如「重点是」「建议你先…」），不要用星号包文字。\n"
    )
    if phase == "out_of_scope":
        common += (
            f"\n【当前阶段：范围外】用户本轮问题不在你的咨询范围内。"
            f"不要回答该题的具体内容（不要写代码、解题、科普跑题知识）。"
            f"简短、友好地说明你是顾问{name}，只能聊感情与关系，并请对方换一个相关问题。"
            f"禁止提及底层模型或厂商名称。"
            f"全文控制在三四句以内。"
        )
        return common

    common += (
        f"\n【追问与建议规则（必须遵守）】\n{guide}\n"
        "额外硬性约束：\n"
        "- 若本轮是追问阶段：只补关键缺口，每次回复里问句不超过 3 个；"
        "先短回应再提问，整段也要短；可鼓励用户上传聊天截图或粘贴关键对话原文。\n"
        "- 若本轮是建议阶段：分段列举可执行建议，本轮最多 4 条；说清就停，不要拉成长文报告。\n"
        "- 用户说「直接说 / 不用问了」等时，必须进入建议，不再追问。\n"
        "- 全程不要偏离亲密关系主题去回答无关知识。\n"
    )
    if phase == "clarify":
        common += (
            "\n【当前阶段：追问】信息仍明显不足。不要给完整长建议；"
            "先一两句短回应理解，再提出不超过 3 个关键问题；整段保持短聊节奏。"
        )
    else:
        common += (
            "\n【当前阶段：正式建议】背景已够或用户要求直接建议。"
            "给出有针对性的分段建议，列举最多 4 条，说清就停；"
            "若有笔记摘录则优先结合摘录，但不要说「没有相关信息」。"
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
) -> tuple[str, dict[str, Any]]:
    """Return (content, usage_info). usage_info may be empty."""
    from app.chat import effective_chat_model, uses_kimi_chat

    empty_usage: dict[str, Any] = {}
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
        return "", empty_usage

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
            return "", empty_usage
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        text = str(content).strip()
        if usage_raw:
            usage = {
                "model": model,
                "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
                "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
                "total_tokens": int(
                    usage_raw.get("total_tokens")
                    or (
                        int(usage_raw.get("prompt_tokens") or 0)
                        + int(usage_raw.get("completion_tokens") or 0)
                    )
                ),
                "estimated": False,
            }
        else:
            # Rough char-based fallback when provider omits usage.
            approx_out = max(1, len(text) // 2) if text else 0
            approx_in = max(1, sum(len(str(m.get("content") or "")) for m in messages) // 2)
            usage = {
                "model": model,
                "prompt_tokens": approx_in,
                "completion_tokens": approx_out,
                "total_tokens": approx_in + approx_out,
                "estimated": True,
            }
        return text, usage


async def decide_phase(
    *,
    settings: Settings,
    user_message: str,
    history: list[dict[str, str]],
    context_summary: str,
    has_images: bool,
    questions_guide: str,
) -> tuple[Phase, dict[str, Any]]:
    user_turns = sum(1 for m in history if m.get("role") == "user")

    hist_snip = []
    for m in history[-8:]:
        role = m.get("role", "")
        content = (m.get("content") or "")[:400]
        hist_snip.append(f"{role}: {content}")
    hist_text = "\n".join(hist_snip) if hist_snip else "(无)"
    summary = context_summary.strip() or "(无)"

    # Identity / "who are you" is in-scope meta about the consultant — answer as advise.
    if is_identity_question(user_message):
        return "advise", {}

    prompt = (
        "你是亲密关系咨询的路由分类器。只输出一个 JSON："
        '{"phase":"out_of_scope"} 或 {"phase":"clarify"} 或 {"phase":"advise"}。\n'
        "判定优先级：\n"
        "1) out_of_scope：本轮主诉求与亲密关系/恋爱/婚姻/择偶/相处/情感沟通无关"
        "（如写代码、数学题、时政、旅游攻略、纯闲聊百科等）。有聊天截图且内容是感情互动则不算跑题。"
        "注意：询问顾问「你是谁 / 真实身份 / 自我介绍」不算跑题，应判 advise。\n"
        "2) clarify：在范围内，但关键背景明显不足，应先追问。\n"
        "3) advise：在范围内，信息已够或用户要求直接给建议；或用户在问顾问身份/自我介绍。\n"
        f"规则摘要：\n{questions_guide[:2800]}\n\n"
        f"上下文摘要：\n{summary}\n\n"
        f"最近对话：\n{hist_text}\n\n"
        f"用户本轮：{user_message[:1500]}\n"
        f"本轮是否附带截图：{'是' if has_images else '否'}\n"
        f"用户是否要求跳过追问：{'是' if wants_skip_clarify(user_message) else '否'}\n"
    )
    raw, usage = await _chat_json(
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
        if is_identity_question(user_message):
            return "advise", usage
        if phase != "out_of_scope" and wants_skip_clarify(user_message):
            return "advise", usage
        return phase, usage

    # Heuristic fallback
    if is_identity_question(user_message):
        return "advise", usage
    if user_turns == 0 and not context_summary.strip() and len(user_message.strip()) < 80:
        return "clarify", usage
    if wants_skip_clarify(user_message):
        return "advise", usage
    if user_turns >= 4 or (context_summary.strip() and user_turns >= 2):
        return "advise", usage
    return "clarify", usage


def _looks_sparse(text: str) -> bool:
    return len((text or "").strip()) < 40


def _parse_phase_json(raw: str) -> Phase | None:
    if not raw:
        return None
    m = re.search(r"\{[^{}]*\}", raw)
    if not m:
        low = raw.lower()
        if "out_of_scope" in low or "out-of-scope" in low or "跑题" in raw:
            return "out_of_scope"
        if "advise" in low and "clarify" not in low:
            return "advise"
        if "clarify" in low:
            return "clarify"
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    phase = str(obj.get("phase", "")).strip().lower().replace("-", "_")
    if phase in ("clarify", "advise", "out_of_scope"):
        return phase  # type: ignore[return-value]
    return None


async def refresh_context_summary(
    *,
    settings: Settings,
    previous_summary: str,
    history: list[dict[str, str]],
    latest_user: str,
    latest_assistant: str,
) -> tuple[str, dict[str, Any]]:
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
    out, usage = await _chat_json(
        settings,
        [
            {"role": "system", "content": "你是咨询上下文压缩助手。只输出摘要。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=600,
    )
    return (out or previous_summary.strip()), usage


async def refresh_user_profile_memory(
    *,
    settings: Settings,
    previous_profile: str,
    latest_user: str,
    latest_assistant: str,
    thread_summary: str = "",
    phase: str = "advise",
) -> tuple[str, dict[str, Any]]:
    """Update shared user profile memory (facts only, no expert advice)."""
    prompt = (
        "你在维护用户的「共享画像记忆」，可在不同专家顾问之间复用。\n"
        "只保留用户自身与重要他人的客观事实与状态，不要写入任何顾问给出的建议条文。\n"
        "要求：\n"
        "1) 结构化中文，建议标题：用户自身 / 重要他人 / 进行中的议题。\n"
        "2) 重要他人按称呼分条；写清关系阶段与关键事实。\n"
        "3) 合并旧画像与本轮新事实；不要编造。全文尽量不超过 2000 字。\n"
        "4) 直接输出画像正文。\n\n"
        f"阶段：{phase}\n"
        f"旧画像：\n{previous_profile.strip() or '(无)'}\n\n"
        f"本线程摘要（可空）：\n{thread_summary.strip() or '(无)'}\n\n"
        f"最新用户：{latest_user[:1500]}\n"
        f"最新助手（仅用于抽取用户事实，勿照抄建议）：{latest_assistant[:1500]}\n"
    )
    out, usage = await _chat_json(
        settings,
        [
            {"role": "system", "content": "你是用户画像压缩助手。只输出画像正文，不含建议。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=700,
    )
    text = (out or previous_profile).strip()
    cap = int(settings.memory_max_chars or 4000)
    if cap > 0 and len(text) > cap:
        text = text[:cap]
    return text, usage


async def refresh_expert_advice_memory(
    *,
    settings: Settings,
    previous_advice: str,
    latest_user: str,
    latest_assistant: str,
    thread_summary: str = "",
    phase: str = "advise",
    expert_name: str = "顾问",
) -> tuple[str, dict[str, Any]]:
    """Update per-expert advice memory (advice only, no shared profile dump)."""
    prompt = (
        f"你在维护专家「{expert_name}」对这位用户的「已给建议记忆」。\n"
        "只保留该专家已经给出的建议要点与行动方案，不要写入用户完整画像（对方年龄等事实从略）。\n"
        "要求：\n"
        "1) 结构化中文分条；合并旧建议与本轮新建议；过时可删。\n"
        "2) 不要编造。全文尽量不超过 2000 字。\n"
        "3) 直接输出建议记忆正文。\n\n"
        f"阶段：{phase}\n"
        f"旧建议记忆：\n{previous_advice.strip() or '(无)'}\n\n"
        f"本线程摘要（可空）：\n{thread_summary.strip() or '(无)'}\n\n"
        f"最新用户：{latest_user[:800]}\n"
        f"最新助手：{latest_assistant[:2000]}\n"
    )
    out, usage = await _chat_json(
        settings,
        [
            {"role": "system", "content": "你是专家建议记忆压缩助手。只输出建议要点。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=700,
    )
    text = (out or previous_advice).strip()
    cap = int(settings.memory_max_chars or 4000)
    if cap > 0 and len(text) > cap:
        text = text[:cap]
    return text, usage


async def refresh_user_memory(
    *,
    settings: Settings,
    previous_memory: str,
    latest_user: str,
    latest_assistant: str,
    thread_summary: str = "",
    phase: str = "advise",
) -> tuple[str, dict[str, Any]]:
    """Legacy combined memory updater (kept for compatibility). Prefer split refreshers."""
    return await refresh_user_profile_memory(
        settings=settings,
        previous_profile=previous_memory,
        latest_user=latest_user,
        latest_assistant=latest_assistant,
        thread_summary=thread_summary,
        phase=phase,
    )


def should_update_user_memory(*, phase: str, user_text: str, assistant_text: str) -> bool:
    """advise always (with reply); clarify when user shared substantive facts."""
    if phase == "out_of_scope":
        return False
    if not (assistant_text or "").strip():
        return False
    if phase == "advise":
        return True
    # clarify: only if user said something with some substance
    return len((user_text or "").strip()) >= 20


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
