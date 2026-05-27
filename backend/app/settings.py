import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    explicit = os.getenv("PROJECT_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_project_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Only notes under this folder are indexed. Override via VAULT_PATH in .env.
    vault_path: Path = Path("notes")
    data_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", str(_project_root() / "data")))
    )

    # Hosted public app: hide sources/citations in UI; disable rebuild from browser.
    public_deploy: bool = False
    # Optional HTTPS URL to a zip of pre-built data/ (chunks + embeddings) for production.
    index_bundle_url: str = ""

    # When set, visitors must log in (cookie session). Use APP_PASSWORD in deploy env_vars / .env.
    app_password: str = ""
    session_secret: str = ""
    cookie_secure: bool = False

    ai_builder_token: str = ""
    ai_api_base_url: str = "https://space.ai-builders.com/backend/v1"
    ai_chat_model: str = "deepseek"
    ai_embedding_model: str = "text-embedding-3-small"

    # Optional: when direct access to space.ai-builders.com fails (e.g. regional network).
    http_proxy: str = ""
    https_proxy: str = ""

    exclude_globs: tuple[str, ...] = (".obsidian/**", ".trash/**", "**/node_modules/**")

    # Tag routing: each tag scored as blend lexical(query,tag) + embedding(query,tag).
    tag_routing_enabled: bool = True
    tag_routing_top_n: int = 3
    tag_routing_pool: int = 56
    tag_routing_min_chunks: int = 6
    tag_routing_absolute_min_chunks: int = 3
    tag_route_lex_weight: float = 0.7
    tag_route_emb_weight: float = 0.3
    tag_route_combined_floor: float = 0.14
    # If best fused tag beats second by at least this gap, scope to the single best tag
    # (avoids OR-widening the pool with unrelated runner-up tags).
    tag_route_scope_primary_gap: float = 0.08

    # Retrieval: cap how many chunks may come from one note before others get a turn;
    # then backfill to retrieve_top_k so context is not empty.
    retrieve_top_k: int = 12
    retrieve_max_chunks_per_note: int = 4


def get_settings() -> Settings:
    return Settings()
