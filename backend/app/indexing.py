from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.embeddings import EmbeddingsUnavailableError, embed_texts
from app.experts import expert_has_pack_knowledge, list_expert_packs, load_expert_pack
from app.ingest import Chunk, load_chunks_for_expert, load_vault_chunks
from app.settings import Settings, get_settings
from app.tag_router import unique_tags_from_chunks
from app.vector_store import (
    embeddings_npz_path,
    save_chunk_embeddings,
    save_normalized_embeddings,
    tag_embeddings_npz_path,
)

_index_lock = Lock()


def _chunks_path(data_dir: Path) -> Path:
    return data_dir / "chunks.jsonl"


def _meta_path(data_dir: Path) -> Path:
    return data_dir / "index_meta.json"


def persist_chunks_jsonl(chunks: list[Chunk], data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _chunks_path(data_dir)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(
                json.dumps(
                    {
                        "id": c.id,
                        "note_path": c.note_path,
                        "note_title": c.note_title,
                        "heading_path": c.heading_path,
                        "text": c.text,
                        "tags": c.tags,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def load_chunks_from_disk(data_dir: Path) -> list[Chunk]:
    path = _chunks_path(data_dir)
    if not path.is_file():
        return []
    chunks: list[Chunk] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj: dict[str, Any] = json.loads(line)
            chunks.append(
                Chunk(
                    id=obj["id"],
                    note_path=obj["note_path"],
                    note_title=obj["note_title"],
                    heading_path=obj["heading_path"],
                    text=obj["text"],
                    tags=list(obj.get("tags") or []),
                )
            )
    return chunks


def write_index_meta(
    data_dir: Path,
    *,
    chunk_count: int,
    vector_enabled: bool,
    error: str | None = None,
    tag_count: int = 0,
    tag_routing_ready: bool = False,
) -> dict[str, Any]:
    meta = {
        "last_indexed_at": datetime.now(UTC).isoformat(),
        "chunk_count": chunk_count,
        "vector_enabled": vector_enabled,
        "error": error,
        "tag_count": tag_count,
        "tag_routing_ready": bool(tag_routing_ready),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(data_dir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def read_index_meta(data_dir: Path) -> dict[str, Any]:
    path = _meta_path(data_dir)
    if not path.is_file():
        return {
            "last_indexed_at": None,
            "chunk_count": 0,
            "vector_enabled": False,
            "ready": False,
            "error": None,
            "tag_count": 0,
            "tag_routing_ready": False,
        }
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    meta["ready"] = meta.get("chunk_count", 0) > 0
    meta.setdefault("tag_count", 0)
    meta.setdefault("tag_routing_ready", False)
    return meta


async def _embed_and_write_index(
    chunks: list[Chunk],
    data_dir: Path,
    settings: Settings,
) -> dict[str, Any]:
    persist_chunks_jsonl(chunks, data_dir)

    ep = embeddings_npz_path(data_dir)
    if ep.is_file():
        ep.unlink()

    tep = tag_embeddings_npz_path(data_dir)
    if tep.is_file():
        tep.unlink()

    vector_enabled = False
    err: str | None = None
    tag_vocabulary_count = len(unique_tags_from_chunks(chunks))
    tag_routing_ready = False

    if chunks:
        try:
            embeddings = await embed_texts(settings, [c.search_blob() for c in chunks])
            save_chunk_embeddings([c.id for c in chunks], embeddings, data_dir)
            vector_enabled = True
            tag_strings = unique_tags_from_chunks(chunks)
            if tag_strings:
                try:
                    t_emb = await embed_texts(settings, tag_strings)
                    save_normalized_embeddings(
                        tag_strings,
                        t_emb,
                        npz_path=tag_embeddings_npz_path(data_dir),
                    )
                    tag_routing_ready = True
                except (asyncio.CancelledError, EmbeddingsUnavailableError):
                    raise
                except Exception:
                    tag_routing_ready = False

        except asyncio.CancelledError:
            raise
        except EmbeddingsUnavailableError as e:
            vector_enabled = False
            err = str(e)
        except Exception as e:  # noqa: BLE001
            vector_enabled = False
            err = str(e)

    meta = write_index_meta(
        data_dir,
        chunk_count=len(chunks),
        vector_enabled=bool(vector_enabled),
        error=err,
        tag_count=tag_vocabulary_count,
        tag_routing_ready=bool(tag_routing_ready),
    )
    meta["embedding_note"] = (
        None
        if vector_enabled
        else "Vectors disabled — BM25 retrieval only (embeddings API unavailable or failed)."
    )
    return meta


async def rebuild_expert_index_async(
    expert_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build index for one expert into data/experts/{slug}/."""
    settings = settings or get_settings()
    pack = load_expert_pack(expert_id, settings)
    if not pack:
        raise FileNotFoundError(f"Expert pack not found: {expert_id}")

    data_dir = settings.data_dir.resolve() / "experts" / pack.slug
    with _index_lock:
        chunks = load_chunks_for_expert(pack.slug, settings)
        meta = await _embed_and_write_index(chunks, data_dir, settings)
        meta["expert_id"] = pack.id
        meta["expert_slug"] = pack.slug
        meta["knowledge_source"] = (
            "pack" if expert_has_pack_knowledge(pack) else "vault"
        )
        return meta


async def rebuild_index_async(settings: Settings | None = None) -> dict[str, Any]:
    """
    Rebuild indexes:
    - Legacy afu vault → data/ (backward compatible)
    - Each pack that has knowledge.md / knowledge/ → data/experts/{slug}/
    """
    settings = settings or get_settings()
    data_dir = settings.data_dir.resolve()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    with _index_lock:
        # Keep afu legacy index at data/ root from Obsidian vault.
        chunks = load_vault_chunks(settings)
        meta = await _embed_and_write_index(chunks, data_dir, settings)
        meta["expert_id"] = settings.default_expert_id
        meta["knowledge_source"] = "vault"

    experts_meta: dict[str, Any] = {}
    for pack in list_expert_packs(settings, enabled_only=False):
        if not expert_has_pack_knowledge(pack):
            continue
        try:
            experts_meta[pack.slug] = await rebuild_expert_index_async(pack.slug, settings)
        except FileNotFoundError as e:
            experts_meta[pack.slug] = {"ready": False, "error": str(e)}

    meta["experts"] = experts_meta
    return meta


def rebuild_index_sync(settings: Settings | None = None) -> dict[str, Any]:
    return asyncio.run(rebuild_index_async(settings))
