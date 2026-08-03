from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.settings import Settings, get_settings, _project_root


@dataclass(frozen=True)
class ExpertPack:
    id: str
    display_name: str
    avatar_label: str
    short_bio: str
    enabled: bool
    scope: str
    root: Path
    persona: str
    questions_guide: str

    @property
    def slug(self) -> str:
        """Filesystem-safe directory name used for index paths."""
        return self.root.name


def experts_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = Path(settings.experts_root) if settings.experts_root else (_project_root() / "experts")
    if not root.is_absolute():
        root = _project_root() / root
    return root


def _read_text(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _load_manifest(dir_path: Path) -> dict | None:
    path = dir_path / "manifest.json"
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not str(obj.get("id") or "").strip():
        return None
    return obj


def _pack_from_dir(dir_path: Path) -> ExpertPack | None:
    manifest = _load_manifest(dir_path)
    if not manifest:
        return None
    persona = _read_text(dir_path / "persona.md")
    questions = _read_text(dir_path / "Questions.md")
    if not questions:
        legacy = _project_root() / "Questions.md"
        if legacy.is_file():
            questions = legacy.read_text(encoding="utf-8").strip()
    if not persona:
        persona = (
            f"你是「{manifest.get('display_name') or dir_path.name}」顾问。"
            "语气温暖务实，像真人微信聊天。"
        )
    return ExpertPack(
        id=str(manifest.get("id") or dir_path.name).strip(),
        display_name=str(manifest.get("display_name") or dir_path.name).strip(),
        avatar_label=str(
            manifest.get("avatar_label") or manifest.get("display_name") or dir_path.name
        ).strip(),
        short_bio=str(manifest.get("short_bio") or "").strip(),
        enabled=bool(manifest.get("enabled", True)),
        scope=str(manifest.get("scope") or "intimate_relationship").strip(),
        root=dir_path,
        persona=persona,
        questions_guide=questions
        or "信息明显不足时先追问（每次不超过 3 问），够了再给建议。",
    )


def list_expert_packs(
    settings: Settings | None = None,
    *,
    enabled_only: bool = False,
) -> list[ExpertPack]:
    settings = settings or get_settings()
    root = experts_root(settings)
    if not root.is_dir():
        return []
    packs: list[ExpertPack] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        pack = _pack_from_dir(child)
        if not pack:
            continue
        if enabled_only and not pack.enabled:
            continue
        packs.append(pack)
    return packs


def load_expert_pack(expert_id: str, settings: Settings | None = None) -> ExpertPack | None:
    """Load by folder slug or by manifest id."""
    settings = settings or get_settings()
    wanted = (expert_id or "").strip() or settings.default_expert_id
    root = experts_root(settings)
    direct = root / wanted
    if direct.is_dir():
        pack = _pack_from_dir(direct)
        if pack:
            return pack
    for pack in list_expert_packs(settings, enabled_only=False):
        if pack.id == wanted or pack.slug == wanted:
            return pack
    return None


def resolve_expert(expert_id: str | None, settings: Settings | None = None) -> ExpertPack:
    """Return enabled pack; fall back to default expert, then first enabled."""
    settings = settings or get_settings()
    wanted = (expert_id or "").strip() or settings.default_expert_id
    pack = load_expert_pack(wanted, settings)
    if pack and pack.enabled:
        return pack
    default = load_expert_pack(settings.default_expert_id, settings)
    if default and default.enabled:
        return default
    enabled = list_expert_packs(settings, enabled_only=True)
    if enabled:
        return enabled[0]
    from app.consult import PERSONA_AND_SCOPE, load_questions_guide

    return ExpertPack(
        id="afu",
        display_name="阿FU",
        avatar_label="阿FU",
        short_bio="亲密关系与情感相处顾问",
        enabled=True,
        scope="intimate_relationship",
        root=experts_root(settings) / "afu",
        persona=PERSONA_AND_SCOPE,
        questions_guide=load_questions_guide(),
    )


def expert_knowledge_md(pack: ExpertPack) -> Path:
    return pack.root / "knowledge.md"


def expert_knowledge_dir(pack: ExpertPack) -> Path:
    return pack.root / "knowledge"


def expert_has_pack_knowledge(pack: ExpertPack) -> bool:
    md = expert_knowledge_md(pack)
    if md.is_file() and md.stat().st_size > 0:
        return True
    kdir = expert_knowledge_dir(pack)
    if kdir.is_dir() and any(kdir.rglob("*.md")):
        return True
    return False


def expert_data_dir(expert_id: str, settings: Settings | None = None) -> Path:
    """
    Per-expert index directory under DATA_DIR/experts/{slug}.
    For afu, fall back to legacy DATA_DIR root if expert subdir has no chunks yet.
    """
    settings = settings or get_settings()
    base = settings.data_dir.resolve()
    pack = load_expert_pack(expert_id, settings)
    slug = pack.slug if pack else ((expert_id or settings.default_expert_id).strip() or "afu")
    dedicated = base / "experts" / slug
    chunks = dedicated / "chunks.jsonl"
    if chunks.is_file():
        return dedicated
    if slug == "afu" or (pack and pack.id == settings.default_expert_id):
        legacy = base / "chunks.jsonl"
        if legacy.is_file():
            return base
    return dedicated


def experts_public_list(settings: Settings | None = None) -> list[dict]:
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "avatar_label": p.avatar_label,
            "short_bio": p.short_bio,
        }
        for p in list_expert_packs(settings, enabled_only=True)
    ]
