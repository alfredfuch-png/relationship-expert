from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi

from app.embeddings import embed_texts
from app.indexing import load_chunks_from_disk
from app.ingest import Chunk
from app.settings import Settings, get_settings
from app.tag_router import (
    chunk_matches_route_tags,
    filter_chunks_by_tags,
    infer_route_tags_for_query,
    pick_route_tags_fused,
)
from app.tokenize import simple_tokens
from app.vector_store import load_chunk_embeddings, query_top_similar

VEC_CANDIDATES = 72
BM25_CANDIDATES = 96
RRF_K = 60.0


@dataclass
class RetrievedChunk:
    id: str
    note_path: str
    note_title: str
    heading_path: str
    text: str
    score: float
    source: str


def _load_chunks(settings: Settings) -> list[Chunk]:
    chunks = load_chunks_from_disk(settings.data_dir.resolve())
    if not chunks:
        raise RuntimeError(
            "Index is empty. Click “Build index” in the sidebar (vault path must exist)."
        )
    return chunks


def bm25_scores(query: str, pool: list[Chunk]) -> list[tuple[float, Chunk]]:
    tokenized_corpus = [simple_tokens(c.search_blob()) for c in pool]
    bm25 = BM25Okapi(tokenized_corpus)
    q_tokens = simple_tokens(query)
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(scores, pool), key=lambda x: x[0], reverse=True)
    return ranked


def _reciprocal_rank_fusion(
    rank_lists: list[list[str]],
    *,
    k: float,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ids in rank_lists:
        if not ids:
            continue
        for rank, cid in enumerate(ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _prioritize_by_route_tags(
    fused: list[tuple[str, float]],
    *,
    chunk_by_id: dict[str, Chunk],
    route_tags: set[str],
    max_take: int,
) -> list[tuple[str, float]]:
    if not route_tags:
        return fused[:max_take]
    yes: list[tuple[str, float]] = []
    nope: list[tuple[str, float]] = []
    for cid, sc in fused:
        ch = chunk_by_id.get(cid)
        if not ch:
            continue
        if chunk_matches_route_tags(ch.tags, route_tags):
            yes.append((cid, sc))
        else:
            nope.append((cid, sc))
    merged = yes + nope
    return merged[: max_take]


def _diverse_top_chunks(
    fused: list[tuple[str, float]],
    *,
    chunk_by_id: dict[str, Chunk],
    top_k: int,
    max_per_note: int,
) -> list[tuple[str, float]]:
    """
    Prefer spreading top_k slots across many notes (avoids one long note owning the context).
    First pass: take in score order with at most max_per_note chunks per note_path.
    Second pass: if still short of top_k, add next-best chunks (any note) not yet taken.
    """
    if top_k <= 0:
        return []
    if max_per_note <= 0:
        return fused[:top_k]

    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    per_note: dict[str, int] = {}

    def try_take(cid: str, sc: float, *, respect_cap: bool) -> bool:
        ch = chunk_by_id.get(cid)
        if not ch or cid in seen:
            return False
        path = ch.note_path
        if respect_cap and per_note.get(path, 0) >= max_per_note:
            return False
        out.append((cid, sc))
        seen.add(cid)
        per_note[path] = per_note.get(path, 0) + 1
        return True

    for cid, sc in fused:
        if len(out) >= top_k:
            break
        try_take(cid, sc, respect_cap=True)

    if len(out) < top_k:
        for cid, sc in fused:
            if len(out) >= top_k:
                break
            try_take(cid, sc, respect_cap=False)

    return out


async def retrieve_context(
    query: str,
    *,
    settings: Settings | None = None,
    top_k: int | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[list[RetrievedChunk], dict[str, Any]]:
    settings = settings or get_settings()
    meta = meta or {}
    effective_top_k = top_k if top_k is not None else settings.retrieve_top_k
    max_per_note = max(0, int(settings.retrieve_max_chunks_per_note))
    data_dir = settings.data_dir.resolve()

    chunks = _load_chunks(settings)
    chunk_by_id = {c.id: c for c in chunks}

    routing: dict[str, Any] = {
        "tag_routing": bool(settings.tag_routing_enabled),
        "tag_routing_ready": bool(meta.get("tag_routing_ready")),
        "applied_tags": [],
        "tag_scores": {},
        "scoped": False,
        "scoped_chunk_count": 0,
        "fallback_reason": None,
        "retrieve_top_k": effective_top_k,
        "retrieve_max_chunks_per_note": max_per_note,
        "distinct_notes_in_context": 0,
    }

    work = chunks
    allowed_ids: set[str] | None = None
    route_tag_set: set[str] = set()

    if settings.tag_routing_enabled and meta.get("tag_routing_ready"):
        ranked_tags, tag_vocab, _ok = await infer_route_tags_for_query(
            query,
            settings=settings,
            data_dir=data_dir,
            routing_ready=True,
        )
        if not ranked_tags and not tag_vocab:
            routing["fallback_reason"] = "no_tag_embedding_match"
        elif tag_vocab:
            picked = pick_route_tags_fused(query, ranked_tags, tag_vocab, settings)
            if picked:
                gap = float(getattr(settings, "tag_route_scope_primary_gap", 0.0))
                if gap > 0 and len(picked) >= 2:
                    ordered = sorted(picked.items(), key=lambda kv: kv[1], reverse=True)
                    best_s = ordered[0][1]
                    second_s = ordered[1][1]
                    if best_s - second_s >= gap:
                        picked = {ordered[0][0]: ordered[0][1]}
                        routing["scope_narrowed_to_primary"] = True

                route_tag_set = set(picked.keys())
                routing["tag_scores"] = dict(
                    sorted(picked.items(), key=lambda kv: kv[1], reverse=True)
                )
                routing["applied_tags"] = list(routing["tag_scores"].keys())

                filtered = filter_chunks_by_tags(chunks, route_tag_set)
                routing["scoped_chunk_count"] = len(filtered)

                abs_min = max(2, getattr(settings, "tag_routing_absolute_min_chunks", 3))
                soft_need = getattr(settings, "tag_routing_min_chunks", 6)

                use_scoped = False
                if len(filtered) >= soft_need:
                    use_scoped = True
                elif len(filtered) >= abs_min:
                    routing["fallback_reason"] = "scoped_below_ideal_but_kept"
                    use_scoped = True

                if use_scoped:
                    work = filtered
                    allowed_ids = {c.id for c in filtered}
                    routing["scoped"] = True
                else:
                    routing["fallback_reason"] = "scoped_pool_too_small_using_global_but_tag_boost"
                    work = chunks
                    allowed_ids = None
            else:
                routing["fallback_reason"] = "no_tags_after_merge"
                work = chunks
    elif settings.tag_routing_enabled and not meta.get("tag_routing_ready"):
        routing["fallback_reason"] = "rebuild_index_for_tag_router"

    bm25_ordered = bm25_scores(query, work)[:BM25_CANDIDATES]
    bm25_ids = [c.id for _, c in bm25_ordered]

    vector_enabled = bool(meta.get("vector_enabled"))
    loaded = load_chunk_embeddings(data_dir)

    vector_ids: list[str] = []
    if vector_enabled and loaded is not None:
        emb_matrix, emb_ids = loaded
        try:
            q_emb = (await embed_texts(settings, [query]))[0]
            vector_ids = [
                cid
                for cid, _ in query_top_similar(
                    q_emb,
                    emb_matrix,
                    emb_ids,
                    top_k=min(VEC_CANDIDATES, len(emb_ids)),
                    allowed_ids=allowed_ids,
                )
            ]
        except Exception:  # noqa: BLE001
            vector_ids = []

    rank_lists = [lst for lst in (vector_ids, bm25_ids) if lst]
    if not rank_lists:
        return [], routing

    fused = _reciprocal_rank_fusion(rank_lists, k=RRF_K)

    if route_tag_set:
        fused = _prioritize_by_route_tags(
            fused,
            chunk_by_id=chunk_by_id,
            route_tags=route_tag_set,
            max_take=max(effective_top_k * 6, 48),
        )

    picked_pairs = _diverse_top_chunks(
        fused,
        chunk_by_id=chunk_by_id,
        top_k=effective_top_k,
        max_per_note=max_per_note,
    )

    vector_set = set(vector_ids[:25])
    bm25_set = set(bm25_ids[:25])

    out: list[RetrievedChunk] = []
    for cid, rr in picked_pairs:
        base = chunk_by_id.get(cid)
        if not base:
            continue
        in_v = cid in vector_set
        in_b = cid in bm25_set
        if in_v and in_b:
            src = "vector+bm25"
        elif in_v:
            src = "vector"
        else:
            src = "bm25"
        out.append(
            RetrievedChunk(
                id=base.id,
                note_path=base.note_path,
                note_title=base.note_title,
                heading_path=base.heading_path,
                text=base.text,
                score=float(rr),
                source=src,
            )
        )

    paths = {c.note_path for c in out}
    routing["distinct_notes_in_context"] = len(paths)

    return out, routing
