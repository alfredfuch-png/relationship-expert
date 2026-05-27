from __future__ import annotations

import hashlib
import fnmatch
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from app.settings import Settings


@dataclass
class Chunk:
    id: str
    note_path: str
    note_title: str
    heading_path: str
    text: str
    tags: list[str]

    def search_blob(self) -> str:
        """Text used for retrieval (BM25 + embeddings). Keeps answers grounded in note body."""
        tag_line = " ".join(self.tags)
        # Title/path headings help match questions that mirror filenames (Obsidian UX).
        return (
            f"{self.note_title}\n{self.note_path}\n{tag_line}\n"
            f"{self.heading_path}\n{self.text}"
        )


def _should_skip(path: Path, vault: Path, patterns: tuple[str, ...]) -> bool:
    rel = path.relative_to(vault).as_posix()
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(str(rel), p):
            return True
    return False


def _split_by_headings(body: str) -> list[tuple[str, str]]:
    """Returns list of (heading_path, section_text)."""
    lines = body.splitlines()
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            path = " / ".join(t for _, t in stack) if stack else "(root)"
            sections.append((path, text))
        buf = []

    heading_re = __import__("re").compile(r"^(#{1,6})\s+(.+?)\s*$")

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            buf = []
            continue
        buf.append(line)
    flush()
    return sections


def _chunk_text(note_path: str, title: str, heading_path: str, text: str, tags: list[str]) -> list[Chunk]:
    max_chars = 1600
    overlap = 200
    chunks: list[Chunk] = []

    def add_slice(s: str, suffix: str) -> None:
        raw_id = f"{note_path}|{heading_path}|{suffix}|{hashlib.md5(s.encode()).hexdigest()[:12]}"
        cid = hashlib.md5(raw_id.encode()).hexdigest()
        chunks.append(
            Chunk(
                id=cid,
                note_path=note_path,
                note_title=title,
                heading_path=heading_path,
                text=s.strip(),
                tags=tags,
            )
        )

    t = text.strip()
    if len(t) <= max_chars:
        add_slice(t, "0")
        return chunks

    start = 0
    part = 0
    while start < len(t):
        end = min(start + max_chars, len(t))
        add_slice(t[start:end], str(part))
        if end >= len(t):
            break
        start = end - overlap
        part += 1
    return chunks


def load_vault_chunks(settings: Settings) -> list[Chunk]:
    vault = settings.vault_path.resolve()
    if not vault.is_dir():
        raise FileNotFoundError(f"Vault not found: {vault}")

    all_chunks: list[Chunk] = []
    for md in sorted(vault.rglob("*.md")):
        if _should_skip(md, vault, settings.exclude_globs):
            continue
        try:
            post = frontmatter.loads(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        meta = post.metadata or {}
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        title = meta.get("title") or md.stem
        rel = md.relative_to(vault).as_posix()
        body = post.content or ""
        sections = _split_by_headings(body)
        if not sections:
            sections = [("(whole)", body.strip() or "(empty)")]

        for heading_path, section_text in sections:
            for c in _chunk_text(rel, str(title), heading_path, section_text, [str(x) for x in tags]):
                all_chunks.append(c)
    return all_chunks
