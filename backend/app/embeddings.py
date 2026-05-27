from __future__ import annotations

import json
from typing import Any

import httpx

from app.http_client import async_gateway_client
from app.settings import Settings


class EmbeddingsUnavailableError(RuntimeError):
    """Gateway does not expose a working /embeddings route."""


async def embed_texts(
    settings: Settings,
    texts: list[str],
    *,
    batch_size: int = 48,
) -> list[list[float]]:
    if not settings.ai_builder_token:
        raise EmbeddingsUnavailableError("AI_BUILDER_TOKEN is missing.")

    base = settings.ai_api_base_url.rstrip("/")
    url = f"{base}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.ai_builder_token}",
        "Content-Type": "application/json",
    }
    out: list[list[float]] = []

    async with async_gateway_client(settings, timeout_seconds=120.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await client.post(
                url,
                headers=headers,
                json={"model": settings.ai_embedding_model, "input": batch},
            )
            if resp.status_code >= 400:
                raise EmbeddingsUnavailableError(
                    f"Embeddings HTTP {resp.status_code}: {resp.text[:400]}"
                )
            payload: dict[str, Any] = resp.json()
            data = payload.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise EmbeddingsUnavailableError(
                    f"Unexpected embeddings payload: keys={list(payload.keys())}"
                )
            pairs: list[tuple[int, list[float]]] = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                idx = int(row.get("index", len(pairs)))
                emb = row.get("embedding")
                if isinstance(emb, list):
                    pairs.append((idx, [float(x) for x in emb]))
            pairs.sort(key=lambda x: x[0])
            if len(pairs) != len(batch):
                raise EmbeddingsUnavailableError("Embedding count mismatch in response.")
            out.extend([e for _, e in pairs])

    return out


async def embeddings_probe(settings: Settings) -> bool:
    try:
        vec = await embed_texts(settings, ["ping"])
        return bool(vec and isinstance(vec[0], list))
    except (
        EmbeddingsUnavailableError,
        httpx.HTTPError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ):
        return False
