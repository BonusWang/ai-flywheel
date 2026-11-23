from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import file_sha256


class KnowledgeWriteError(ValueError):
    """Raised when a knowledge write violates allowed prefixes, path traversal, or read-back checks."""


class KnowledgeSinkAdapter:
    """Decoupled knowledge storage adapter supporting built-in KB or mounted local Markdown Wiki."""

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db

    @property
    def root(self) -> Path:
        root = self.cfg.knowledge_root_path
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _safe_resolve(self, relative_path: str) -> Path:
        norm = relative_path.replace("\\", "/").lstrip("/")
        target = (self.root / norm).resolve()
        root_resolved = self.root.resolve()
        if root_resolved not in target.parents and target != root_resolved:
            raise KnowledgeWriteError(f"Path traversal blocked: {relative_path}")

        allowed = [p.replace("\\", "/").lstrip("/") for p in self.cfg.knowledge_sink.allowed_write_prefixes]
        if allowed and not any(norm.startswith(prefix) for prefix in allowed):
            raise KnowledgeWriteError(
                f"Write path '{norm}' is outside allowed_write_prefixes {allowed}"
            )
        return target

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        root_resolved = self.root.resolve()
        for md in sorted(root_resolved.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            rel = md.relative_to(root_resolved).as_posix()
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            title = md.stem
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            docs.append(
                {
                    "doc_path": rel,
                    "title": title,
                    "size_bytes": md.stat().st_size,
                    "excerpt": text[:280],
                }
            )
        return docs

    def read_document(self, relative_path: str) -> dict[str, Any] | None:
        norm = relative_path.replace("\\", "/").lstrip("/")
        target = (self.root / norm).resolve()
        root_resolved = self.root.resolve()
        if root_resolved not in target.parents or not target.exists():
            return None
        content = target.read_text(encoding="utf-8", errors="ignore")
        return {
            "doc_path": norm,
            "content": content,
            "sha256": file_sha256(target),
        }

    def write_document_verified(self, relative_path: str, content: str, action: str = "create") -> dict[str, Any]:
        """Controlled atomic write with preimage/postimage SHA-256 read-back verification."""
        target = self._safe_resolve(relative_path)
        preimage_sha = file_sha256(target) if target.exists() else "ABSENT"

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

        # Read-back verification
        readback = target.read_text(encoding="utf-8")
        postimage_sha = file_sha256(target)
        if readback != content:
            raise KnowledgeWriteError(f"Read-back content mismatch on {relative_path}")
        if action == "update" and preimage_sha == postimage_sha:
            raise KnowledgeWriteError(f"Update action produced no byte change on {relative_path}")

        self.sync_fts_index()
        return {
            "relative_path": relative_path.replace("\\", "/").lstrip("/"),
            "absolute_path": str(target),
            "preimage_sha256": preimage_sha,
            "postimage_sha256": postimage_sha,
            "verified": True,
        }

    def sync_fts_index(self) -> int:
        docs: list[dict[str, str]] = []
        root_resolved = self.root.resolve()
        for md in root_resolved.rglob("*.md"):
            try:
                body = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = md.relative_to(root_resolved).as_posix()
            title = md.stem
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            docs.append({"doc_path": rel, "title": title, "body": body[:20000], "tags": rel.split("/")[0]})
        return self.db.rebuild_knowledge_fts(docs)
