"""Emergency risk detection: keyword screen + LLM confirm."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.consult import _chat_json
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Categories stored in risk_alerts.categories JSON.
CATEGORY_SELF_HARM = "self_harm"
CATEGORY_HARM_OTHERS = "harm_others"
CATEGORY_DOMESTIC_VIOLENCE = "domestic_violence"
CATEGORY_OTHER = "other_emergency"

# Keyword → suggested category (substring match, case-insensitive for ASCII).
KEYWORD_CATEGORY: list[tuple[str, str]] = [
    # Self-harm / suicide
    ("自杀", CATEGORY_SELF_HARM),
    ("轻生", CATEGORY_SELF_HARM),
    ("自尽", CATEGORY_SELF_HARM),
    ("不想活", CATEGORY_SELF_HARM),
    ("活不下去", CATEGORY_SELF_HARM),
    ("结束生命", CATEGORY_SELF_HARM),
    ("割腕", CATEGORY_SELF_HARM),
    ("自残", CATEGORY_SELF_HARM),
    ("自伤", CATEGORY_SELF_HARM),
    ("跳楼", CATEGORY_SELF_HARM),
    ("想死", CATEGORY_SELF_HARM),
    ("去死", CATEGORY_SELF_HARM),
    ("一了百了", CATEGORY_SELF_HARM),
    # Harm to others
    ("弄死他", CATEGORY_HARM_OTHERS),
    ("弄死她", CATEGORY_HARM_OTHERS),
    ("杀了他", CATEGORY_HARM_OTHERS),
    ("杀了她", CATEGORY_HARM_OTHERS),
    ("打死他", CATEGORY_HARM_OTHERS),
    ("打死她", CATEGORY_HARM_OTHERS),
    ("砍他", CATEGORY_HARM_OTHERS),
    ("砍她", CATEGORY_HARM_OTHERS),
    ("报复社会", CATEGORY_HARM_OTHERS),
    ("同归于尽", CATEGORY_HARM_OTHERS),
    ("要他的命", CATEGORY_HARM_OTHERS),
    ("要她的命", CATEGORY_HARM_OTHERS),
    # Domestic violence / abuse
    ("家暴", CATEGORY_DOMESTIC_VIOLENCE),
    ("家庭暴力", CATEGORY_DOMESTIC_VIOLENCE),
    ("打我", CATEGORY_DOMESTIC_VIOLENCE),
    ("动手打", CATEGORY_DOMESTIC_VIOLENCE),
    ("殴打", CATEGORY_DOMESTIC_VIOLENCE),
    ("掐我", CATEGORY_DOMESTIC_VIOLENCE),
    ("推搡", CATEGORY_DOMESTIC_VIOLENCE),
    ("跟踪我", CATEGORY_DOMESTIC_VIOLENCE),
    ("威胁我", CATEGORY_DOMESTIC_VIOLENCE),
    ("恐吓我", CATEGORY_DOMESTIC_VIOLENCE),
    ("关起来", CATEGORY_DOMESTIC_VIOLENCE),
    ("不让出门", CATEGORY_DOMESTIC_VIOLENCE),
    ("性侵", CATEGORY_DOMESTIC_VIOLENCE),
    ("强奸", CATEGORY_DOMESTIC_VIOLENCE),
    ("紧急危险", CATEGORY_OTHER),
    ("报警", CATEGORY_OTHER),
]

RISK_CONFIRM_SYSTEM = """你是安全审核助手。根据用户咨询原文，判断是否存在需要人工运营立即关注的紧急人身安全风险。

只在下列真实、当下或近期意图/处境时判 risk=true：
- 自伤/自杀意图或计划
- 伤害他人的意图或计划
- 家暴、殴打、跟踪、拘禁、性侵等紧急危险处境（用户本人正在经历或即将发生）

下列情况必须判 risk=false：
- 新闻/影视/他人故事转述、虚构假设、抽象讨论概念
- 明确否定（如「我不会自杀」「不是家暴」）
- 普通吵架、冷战、情绪低落但无自伤/暴力紧急信号
- 仅提到「报警」但语境是琐事投诉且无人身危险

只输出一个 JSON 对象，不要 Markdown，不要其它说明：
{"risk":true/false,"categories":["self_harm"|"harm_others"|"domestic_violence"|"other_emergency"],"confidence":"high"|"medium","reason":"一句话"}
categories 仅在 risk=true 时填写相关项；risk=false 时 categories 为 []。
"""


def scan_risk_keywords(text: str) -> list[tuple[str, str]]:
    """Return list of (keyword, category) hits."""
    t = (text or "").strip()
    if not t:
        return []
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    lower = t.lower()
    for kw, cat in KEYWORD_CATEGORY:
        needle = kw.lower()
        if needle in lower and kw not in seen:
            hits.append((kw, cat))
            seen.add(kw)
    return hits


def _extract_json_obj(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def confirm_risk_with_llm(
    *,
    user_text: str,
    keyword_hits: list[tuple[str, str]],
    settings: Settings | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Returns (parsed_result_or_None, usage_dict).
    parsed_result keys: risk, categories, confidence, reason
    """
    settings = settings or get_settings()
    hits_str = "、".join(h[0] for h in keyword_hits) or "（无）"
    messages = [
        {"role": "system", "content": RISK_CONFIRM_SYSTEM},
        {
            "role": "user",
            "content": (
                f"初筛命中关键词：{hits_str}\n\n"
                f"用户消息：\n{(user_text or '')[:2000]}"
            ),
        },
    ]
    try:
        raw, usage = await _chat_json(settings, messages, max_tokens=220)
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk LLM confirm failed: %s", exc)
        return None, {}
    obj = _extract_json_obj(raw)
    if not obj:
        logger.warning("risk LLM returned non-JSON: %s", (raw or "")[:200])
        return None, usage
    risk = bool(obj.get("risk"))
    cats_raw = obj.get("categories") or []
    allowed = {
        CATEGORY_SELF_HARM,
        CATEGORY_HARM_OTHERS,
        CATEGORY_DOMESTIC_VIOLENCE,
        CATEGORY_OTHER,
    }
    categories: list[str] = []
    if isinstance(cats_raw, list):
        for c in cats_raw:
            s = str(c).strip()
            if s in allowed and s not in categories:
                categories.append(s)
    if risk and not categories:
        # Fall back to keyword-suggested categories.
        for _kw, cat in keyword_hits:
            if cat not in categories:
                categories.append(cat)
        if not categories:
            categories = [CATEGORY_OTHER]
    conf = str(obj.get("confidence") or "medium").strip().lower()
    if conf not in ("high", "medium"):
        conf = "medium"
    return {
        "risk": risk,
        "categories": categories if risk else [],
        "confidence": conf,
        "reason": str(obj.get("reason") or "").strip()[:300],
    }, usage


async def evaluate_user_message_for_risk(
    *,
    user_text: str,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """
    Full pipeline. Returns alert payload if risk confirmed, else None.
    Payload: categories, confidence, reason, keyword_hits, snippet
    """
    settings = settings or get_settings()
    text = (user_text or "").strip()
    if not text:
        return None
    hits = scan_risk_keywords(text)
    if not hits:
        return None
    logger.info("risk keyword hit: %s", [h[0] for h in hits])
    confirmed, _usage = await confirm_risk_with_llm(
        user_text=text,
        keyword_hits=hits,
        settings=settings,
    )
    if not confirmed or not confirmed.get("risk"):
        return None
    return {
        "categories": list(confirmed.get("categories") or []),
        "confidence": str(confirmed.get("confidence") or "medium"),
        "reason": str(confirmed.get("reason") or ""),
        "keyword_hits": [h[0] for h in hits],
        "snippet": text[:200],
    }
