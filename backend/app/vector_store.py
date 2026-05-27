from __future__ import annotations

import numpy as np
from pathlib import Path


def embeddings_npz_path(data_dir: Path) -> Path:
    return data_dir / "embeddings.npz"


def tag_embeddings_npz_path(data_dir: Path) -> Path:
    return data_dir / "tag_embeddings.npz"


def save_normalized_embeddings(
    ids: list[str],
    vectors: list[list[float]],
    *,
    npz_path: Path,
) -> None:
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms
    ids_arr = np.array(ids, dtype=object)
    np.savez_compressed(npz_path, emb=arr, ids=ids_arr)


def load_normalized_embeddings(npz_path: Path) -> tuple[np.ndarray, list[str]] | None:
    if not npz_path.is_file():
        return None
    data = np.load(npz_path, allow_pickle=True)
    emb = data["emb"].astype(np.float32, copy=False)
    ids = data["ids"].tolist()
    return emb, ids


def load_chunk_embeddings(data_dir: Path) -> tuple[np.ndarray, list[str]] | None:
    return load_normalized_embeddings(embeddings_npz_path(data_dir))


def save_chunk_embeddings(ids: list[str], vectors: list[list[float]], data_dir: Path) -> None:
    save_normalized_embeddings(
        ids,
        vectors,
        npz_path=embeddings_npz_path(data_dir),
    )


def query_top_similar(
    query_embedding: list[float],
    emb_matrix: np.ndarray,
    ids: list[str],
    *,
    top_k: int,
    allowed_ids: set[str] | None = None,
) -> list[tuple[str, float]]:
    q = np.asarray(query_embedding, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    q = q / qn
    sims = emb_matrix @ q
    n = len(ids)
    if allowed_ids is not None:
        # Score all, then rank with filter (fine for vault-scale n).
        order = np.argsort(-sims)
        picked: list[tuple[str, float]] = []
        for i in order:
            cid = str(ids[int(i)])
            if cid not in allowed_ids:
                continue
            picked.append((cid, float(sims[int(i)])))
            if len(picked) >= top_k:
                break
        return picked

    if top_k >= n:
        order = np.argsort(-sims)
    else:
        order = np.argpartition(-sims, top_k)[:top_k]
        order = order[np.argsort(-sims[order])]
    return [(str(ids[int(i)]), float(sims[int(i)])) for i in order]
