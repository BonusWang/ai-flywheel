from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

import yaml

from ai_flywheel.adapters.wiki_gate import WikiGate, WikiGateError, split_frontmatter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.privacy import compute_valid_until, sanitize_and_check_secrets, shanghai_today_ymd
from ai_flywheel.storage.db import FlywheelDB, build_knowledge_search_text
from ai_flywheel.storage.fs_store import file_sha256

# D4/Batch-2 (2026-11-23): default search surface = formal_active partitions
# minus the derived `queries` partition, matching the audit's P-2 spec.
FTS_PARTITION_IDS = {"entities", "topics", "sources", "comparisons", "synthesis", "projects"}
EXCLUDED_STATUSES = {"archived", "deprecated", "retired", "snapshot", "placeholder", "process_data"}
EXCLUDED_PATH_SEGMENTS = {"raw", "raw-原始素材", ".wiki-tmp", ".wiki-agent-memory"}


class KnowledgeWriteError(ValueError):
    """Raised when a knowledge write violates allowed prefixes, path traversal, or read-back checks."""


class KnowledgeSinkAdapter:
    """Decoupled knowledge storage adapter supporting built-in KB or mounted local Markdown Wiki."""

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db
        self.wiki_gate = WikiGate(cfg)

    @property
    def root(self) -> Path:
        root = self.cfg.knowledge_root_path
        root.mkdir(parents=True, exist_ok=True)
        return root

    @property
    def schema_path(self) -> Path:
        return self.root / ".wiki-schema.md"

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

    # ---------------- schema-aware indexing surface ----------------

    def _load_schema(self) -> dict[str, Any] | None:
        if not self.schema_path.is_file():
            return None
        text = self.schema_path.read_text(encoding="utf-8", errors="replace")
        fm_text, _ = split_frontmatter(text)
        if fm_text is None:
            raise KnowledgeWriteError(f"{self.schema_path} has no parseable frontmatter")
        data = yaml.safe_load(fm_text)
        if not isinstance(data, dict):
            raise KnowledgeWriteError(f"{self.schema_path} frontmatter is not a mapping")
        for key in ("partition_contract", "content_contract"):
            if not isinstance(data.get(key), dict):
                raise KnowledgeWriteError(f"{self.schema_path} is missing machine block '{key}'")
        return data

    def _partition_roots(self, schema: dict[str, Any]) -> list[Path]:
        content = schema["content_contract"]
        partition_contract = schema["partition_contract"]
        profile = (content.get("scope_profiles") or {}).get("formal_active") or {}
        declared = [str(x) for x in (profile.get("partition_ids") or [])]
        partitions = partition_contract.get("partitions") or {}
        roots: list[Path] = []
        for partition_id in declared:
            if partition_id not in FTS_PARTITION_IDS:
                continue
            root = str((partitions.get(partition_id) or {}).get("root") or "")
            if root:
                roots.append(self.root.resolve() / root)
        return roots

    @staticmethod
    def _is_excluded_path(relative_path: str) -> bool:
        return any(
            segment in EXCLUDED_PATH_SEGMENTS or segment.startswith("archive-")
            for segment in relative_path.split("/")
        )

    def _iter_candidate_files(self) -> Iterator[tuple[Path, str]]:
        """Yield (path, relative_path) for the indexable surface.

        With a schema, only formal_active partitions are walked.  Without one
        (builtin_kb / tests), the whole root is walked as before.
        """
        root_resolved = self.root.resolve()
        schema = self._load_schema()
        if schema is None:
            paths = sorted(root_resolved.rglob("*.md"))
        else:
            paths = []
            for partition_root in self._partition_roots(schema):
                if partition_root.is_dir():
                    paths.extend(sorted(partition_root.rglob("*.md")))
        for md in paths:
            rel = md.relative_to(root_resolved).as_posix()
            if self._is_excluded_path(rel):
                continue
            yield md, rel

    @staticmethod
    def _parse_page(text: str) -> dict[str, Any] | None:
        """Parse frontmatter; None means the page must not enter default search."""
        fm_text, body = split_frontmatter(text)
        if fm_text is None:
            return None
        try:
            fm = yaml.safe_load(fm_text)
        except yaml.YAMLError:
            return None
        if not isinstance(fm, dict):
            return None
        status = str(fm.get("status") or "").strip().lower()
        if status in EXCLUDED_STATUSES:
            return None
        page_type = str(fm.get("type") or "").strip()
        if page_type == "redirect":
            return None
        sensitivity = str(fm.get("sensitivity") or "").strip().lower()
        if sensitivity == "private_sensitive":
            return None
        if str(fm.get("contains_credentials")).strip().lower() in {"1", "true", "yes", "on"}:
            return None
        return {"frontmatter": fm, "body": body, "page_type": page_type}

    @staticmethod
    def _parse_page_lenient(text: str) -> dict[str, Any]:
        """Non-Wiki roots keep the pre-2026-11-23 behaviour: index every Markdown file."""
        fm_text, body = split_frontmatter(text)
        fm: dict[str, Any] = {}
        if fm_text is not None:
            try:
                loaded = yaml.safe_load(fm_text)
                if isinstance(loaded, dict):
                    fm = loaded
            except yaml.YAMLError:
                fm = {}
        return {
            "frontmatter": fm,
            "body": body,
            "page_type": str(fm.get("type") or ""),
        }

    @staticmethod
    def _title(fm: dict[str, Any], body: str, fallback: str) -> str:
        title = str(fm.get("title") or "").strip()
        if title:
            return title
        for line in body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return fallback

    @staticmethod
    def _tags_text(fm: dict[str, Any]) -> str:
        tags = fm.get("tags")
        if isinstance(tags, list):
            return " ".join(str(tag).strip() for tag in tags if str(tag).strip())
        if isinstance(tags, str):
            return tags.strip()
        return ""

    def _search_documents(self, limit: int | None = None) -> tuple[list[dict[str, str]], int]:
        schema = self._load_schema()
        alias_map = {}
        if schema is not None:
            alias_map = (schema["content_contract"].get("legacy_type_aliases") or {})
        docs: list[dict[str, str]] = []
        skipped = 0
        for md, rel in self._iter_candidate_files():
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                skipped += 1
                continue
            if schema is not None:
                parsed = self._parse_page(text)
                if parsed is None:
                    skipped += 1
                    continue
            else:
                parsed = self._parse_page_lenient(text)
            fm = parsed["frontmatter"]
            body = parsed["body"]
            page_type = parsed["page_type"]
            if page_type in alias_map:
                page_type = str(alias_map[page_type])
            title = self._title(fm, body, md.stem)
            description = str(fm.get("description") or "").strip()
            tags_text = self._tags_text(fm)
            body_text = body[:20000]
            project_id = str(fm.get("project_id") or "").strip()
            if not project_id and rel.startswith("wiki-知识库/projects-项目/"):
                parts = rel.split("/")
                if len(parts) > 2:
                    project_id = parts[2]
            tags_value = tags_text or rel.split("/")[0]
            docs.append(
                {
                    "doc_path": rel,
                    "title": title,
                    "description": description,
                    "body": body_text,
                    "title_index": build_knowledge_search_text(title),
                    "description_index": build_knowledge_search_text(description),
                    "body_index": build_knowledge_search_text(body_text),
                    "tags_index": build_knowledge_search_text(tags_value),
                    "title_raw": title,
                    "description_raw": description,
                    "tags_raw": tags_value,
                    "project_id": project_id,
                    "page_type": page_type,
                    "status": str(fm.get("status") or "").strip(),
                    "freshness_tier": str(fm.get("freshness_tier") or "").strip(),
                    "last_verified": str(fm.get("last_verified") or "").strip(),
                    "valid_until": str(fm.get("valid_until") or "").strip(),
                }
            )
            if limit is not None and len(docs) >= limit:
                break
        return docs, skipped

    # ---------------- public read surface ----------------

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        root_resolved = self.root.resolve()
        files = sorted(root_resolved.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        docs: list[dict[str, Any]] = []
        for md in files:
            rel = md.relative_to(root_resolved).as_posix()
            if self._is_excluded_path(rel):
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parsed = self._parse_page(text) if self.schema_path.is_file() else None
            if self.schema_path.is_file() and parsed is None:
                continue
            fm = parsed["frontmatter"] if parsed else {}
            body = parsed["body"] if parsed else text
            title = self._title(fm, body, md.stem)
            docs.append(
                {
                    "doc_path": rel,
                    "title": title,
                    "size_bytes": md.stat().st_size,
                    "excerpt": body[:280],
                }
            )
            if len(docs) >= limit:
                break
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

    # ---------------- write path ----------------

    def write_document_verified(self, relative_path: str, content: str, action: str = "create") -> dict[str, Any]:
        """Controlled atomic write with privacy/secrets gate, pre-write schema gate, and post-write verification."""
        target = self._safe_resolve(relative_path)
        rel = relative_path.replace("\\", "/").lstrip("/")
        # v3 write-time privacy & secret leak gate (<private> redaction + refuseSecrets)
        content = sanitize_and_check_secrets(content, context=f"knowledge_doc:{rel}")
        gate_result = self.wiki_gate.validate_document(rel, content)
        if gate_result.applicable and not gate_result.ok:
            self.wiki_gate.write_artifact(gate_result, rel, {"stage": "pre_write"})
            raise WikiGateError(_format_gate_failure(rel, gate_result))

        preimage_bytes = target.read_bytes() if target.exists() else None
        preimage_sha = hashlib.sha256(preimage_bytes).hexdigest() if preimage_bytes is not None else "ABSENT"

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

        rollback_info: dict[str, Any] | None = None
        if gate_result.applicable:
            self.wiki_gate.run_topic_lint(gate_result, rel)
            if gate_result.ok:
                self.wiki_gate.run_postflight(gate_result, rel)
            if not gate_result.ok:
                rollback_info = self._rollback(target, preimage_bytes)
                rollback_info["resync"] = self.wiki_gate.resync_after_rollback(rel)
                self.sync_fts_index()
                self.wiki_gate.write_artifact(
                    gate_result,
                    rel,
                    {"stage": "post_write", "rollback": rollback_info},
                )
                raise WikiGateError(_format_gate_failure(rel, gate_result, rollback_info))
            self.wiki_gate.write_artifact(gate_result, rel, {"stage": "post_write"})

        self.sync_fts_index()
        # Record initial freshness governance metadata for the written card
        fm_parsed = self._parse_page_lenient(content).get("frontmatter") or {}
        tier = str(fm_parsed.get("freshness_tier") or "evolving").strip().lower()
        if tier not in {"volatile", "evolving", "evergreen"}:
            tier = "evolving"
        last_verified = str(fm_parsed.get("last_verified") or shanghai_today_ymd()).strip()
        valid_until = str(fm_parsed.get("valid_until") or compute_valid_until(tier, last_verified)).strip()
        self.db.upsert_knowledge_freshness(
            rel,
            freshness_tier=tier,
            last_verified_at=last_verified,
            valid_until=valid_until,
            freshness_state="fresh",
        )
        return {
            "relative_path": rel,
            "absolute_path": str(target),
            "preimage_sha256": preimage_sha,
            "postimage_sha256": postimage_sha,
            "freshness_tier": tier,
            "valid_until": valid_until,
            "verified": True,
            "wiki_gate": gate_result.to_dict() if gate_result.applicable else None,
        }

    @staticmethod
    def _rollback(target: Path, preimage_bytes: bytes | None) -> dict[str, Any]:
        try:
            if preimage_bytes is None:
                target.unlink(missing_ok=True)
                state = "deleted_new_file"
            else:
                tmp = target.with_suffix(target.suffix + ".rollback.tmp")
                tmp.write_bytes(preimage_bytes)
                tmp.replace(target)
                state = "restored_preimage"
        except OSError as exc:
            return {"state": "rollback_failed", "error": str(exc)}
        return {"state": state}

    def sync_fts_index(self) -> int:
        docs, _skipped = self._search_documents()
        count = self.db.rebuild_knowledge_fts(docs)
        existing_map = self.db.get_knowledge_freshness_map()
        for doc in docs:
            tier = doc.get("freshness_tier") or ""
            last_v = doc.get("last_verified") or ""
            valid_u = doc.get("valid_until") or ""
            if not (tier or last_v or valid_u):
                continue
            rel = doc["doc_path"]
            prev = existing_map.get(rel)
            if prev and prev.get("last_verified_at") and last_v and str(prev["last_verified_at"]) >= last_v:
                continue
            effective_tier = tier if tier in {"volatile", "evolving", "evergreen"} else "evolving"
            effective_v = last_v or shanghai_today_ymd()
            effective_u = valid_u or compute_valid_until(effective_tier, effective_v)
            self.db.upsert_knowledge_freshness(
                rel,
                freshness_tier=effective_tier,
                last_verified_at=effective_v,
                valid_until=effective_u,
                freshness_state="fresh",
            )
        return count


def _format_gate_failure(
    relative_path: str,
    result: Any,
    rollback_info: dict[str, Any] | None = None,
) -> str:
    lines = [f"Wiki gate blocked write to '{relative_path}'."]
    for issue in result.issues:
        field_name = f" [{issue['field']}]" if issue.get("field") else ""
        lines.append(f"- {issue['code']}{field_name}: {issue['message']}")
    if result.postflight and result.postflight.get("status") in {"blocked", "fail", "error"}:
        lines.append(f"- postflight: {result.postflight.get('status')} rc={result.postflight.get('returncode')}")
    if rollback_info:
        lines.append(f"- rollback: {rollback_info.get('state')}")
        resync = rollback_info.get("resync") or {}
        if resync:
            lines.append(f"- index resync after rollback: {resync.get('status')}")
    if result.artifact_path:
        lines.append(f"- evidence: {result.artifact_path}")
    return "\n".join(lines)
