from __future__ import annotations

from pathlib import Path

from app.embeddings import embed_texts
from app.ingest import Chunk
from app.settings import Settings
from app.tokenize import simple_tokens
from app.vector_store import load_normalized_embeddings, query_top_similar, tag_embeddings_npz_path


def normalize_tag(t: str) -> str:
    s = str(t).strip()
    if s.startswith("#"):
        s = s[1:]
    return s.strip()


def unique_tags_from_chunks(chunks: list[Chunk]) -> list[str]:
    bag: set[str] = set()
    for c in chunks:
        for raw in c.tags:
            nt = normalize_tag(raw)
            if nt:
                bag.add(nt)
    return sorted(bag)


def chunk_matches_route_tags(note_tags: list[str], route_tags: set[str]) -> bool:
    """
    Exact match between normalized tags, plus Obsidian-style hierarchy prefixes.
    Examples: route '婚恋' matches note tag '婚恋/矛盾'; route 'a/b' matches 'a/b/c'.
    """
    nt_set = {normalize_tag(t) for t in note_tags if normalize_tag(t)}
    if not route_tags or not nt_set:
        return False
    if route_tags & nt_set:
        return True
    for r in route_tags:
        for nt in nt_set:
            if nt.startswith(r + "/") or r.startswith(nt + "/"):
                return True
    return False


def filter_chunks_by_tags(chunks: list[Chunk], route_tags: set[str]) -> list[Chunk]:
    if not route_tags:
        return list(chunks)
    return [c for c in chunks if chunk_matches_route_tags(c.tags, route_tags)]


def lexical_tag_relevance(query: str, tag: str) -> float:
    """
    How well a tag aligns with query text (no embeddings).
    """
    nt = normalize_tag(tag)
    if not nt or not query.strip():
        return 0.0
    q = query.strip()
    score = 0.0

    if len(nt) >= 2:
        longest = min(len(nt), 12)
        for ln in range(longest, 1, -1):
            hit = False
            for i in range(0, len(nt) - ln + 1):
                substr = nt[i : i + ln]
                if len(substr) >= 2 and substr in q:
                    score += 0.35 + 0.04 * ln
                    hit = True
                    break
            if hit:
                break

    q_tokens = set(simple_tokens(q))
    t_tokens = set(simple_tokens(nt))
    if q_tokens and t_tokens:
        inter = q_tokens & t_tokens
        if inter:
            score += 0.25 * (len(inter) / max(len(q_tokens | t_tokens), 1))

    qh = [w for w in ("恋爱", "爱情", "情侣", "失恋", "分手", "相亲", "约会", "择偶", "婚姻", "暧昧") if w in q]
    th = [w for w in ("亲密", "婚姻", "择偶", "恋爱", "相亲", "情侣", "两性", "家庭") if w in nt]
    if qh and th:
        score += min(0.55, 0.12 * min(len(qh), 5) + 0.12 * min(len(th), 4))

    if ("矛盾" in q or "冲突" in q or "吵架" in q) and any(
        x in nt for x in ("亲密", "婚姻", "恋爱", "择偶", "家庭")
    ):
        score += 0.22

    return min(score, 1.0)


def pick_route_tags_fused(
    query: str,
    ranked_embedding: list[tuple[str, float]],
    tag_list: list[str],
    settings: Settings,
) -> dict[str, float]:
    """
    Blend lexical(question,tag) with embedding(question,tag) so irrelevant high-emb tags lose.
    """
    emb_scores = dict(ranked_embedding)
    w_lex = settings.tag_route_lex_weight
    w_emb = settings.tag_route_emb_weight
    floor = settings.tag_route_combined_floor

    fused: dict[str, float] = {}
    for tag in tag_list:
        nt = normalize_tag(tag)
        if not nt:
            continue
        lx = lexical_tag_relevance(query, nt)
        es = float(emb_scores.get(nt, 0.0))
        comb = w_lex * lx + w_emb * es
        if comb < floor:
            continue
        fused[nt] = round(comb, 4)

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[
        : settings.tag_routing_top_n
    ]
    return dict(ordered)


async def infer_route_tags_for_query(
    query: str,
    *,
    settings: Settings,
    data_dir: Path,
    routing_ready: bool,
) -> tuple[list[tuple[str, float]], list[str], bool]:
    """
    Returns (embedding_ranked_tags, full_tag_vocabulary, loaded_ok_after_embed_attempt).
    """
    if not routing_ready:
        return [], [], False

    path = tag_embeddings_npz_path(data_dir)
    loaded = load_normalized_embeddings(path)
    if loaded is None:
        return [], [], False

    emb_matrix, tag_list = loaded
    if not tag_list:
        return [], [], False

    try:
        q_emb = (await embed_texts(settings, [query]))[0]
    except Exception:  # noqa: BLE001
        return [], tag_list, True

    take = min(len(tag_list), max(settings.tag_routing_pool, settings.tag_routing_top_n * 8))
    ranked = query_top_similar(q_emb, emb_matrix, tag_list, top_k=take)
    return ranked, tag_list, True
