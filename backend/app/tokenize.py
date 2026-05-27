import re


def simple_tokens(text: str) -> list[str]:
    """
    Mixed EN/ZH tokenizer for BM25.
    Uses overlapping Chinese bigrams/trigrams so short queries align with prose.
    """
    if not text.strip():
        return []
    lowered = text.lower().strip()

    raw_parts = re.findall(
        r"[\u4e00-\u9fff]{1,2}|[\u4e00-\u9fff]{3,}|\w+(?:'\w+)?",
        lowered,
    )
    out: list[str] = []

    def add_cjk_phrase(seg: str) -> None:
        if len(seg) <= 4:
            for i in range(0, len(seg) - 1):
                out.append(seg[i : i + 2])
            for i in range(0, len(seg) - 2):
                out.append(seg[i : i + 3])
            out.append(seg)
            for ch in seg:
                out.append(ch)
            return

        # Long segments: overlapping 2–3 grams + whole phrase head (captures filenames)
        for i in range(0, len(seg) - 1):
            out.append(seg[i : i + 2])
        for i in range(0, len(seg) - 2):
            out.append(seg[i : i + 3])
        out.append(seg[: min(24, len(seg))])

    for p in raw_parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", p):
            add_cjk_phrase(p)
        elif p.strip():
            out.append(p.strip())

    if not out:
        out = list(lowered)
    dedup_seen: set[str] = set()
    ordered: list[str] = []
    for t in out:
        if t and t not in dedup_seen:
            dedup_seen.add(t)
            ordered.append(t)
    return ordered
