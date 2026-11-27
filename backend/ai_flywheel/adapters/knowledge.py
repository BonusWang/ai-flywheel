from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

import yaml

from ai_flywheel.adapters.wiki import (
    FORBIDDEN_PATH_SEGMENTS,
    PracticeIndexError,
    PracticeIndexRegistration,
    SCHEMA_FILE_NAME,
    UPDATE_NO_NEW_CONTENT,
    WikiGate,
    WikiGateError,
    WikiSchemaLoader,
    approved_registration_matches,
    merge_domain_increment,
    practice_index_write_lock,
    preserves_existing,
    split_frontmatter,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.privacy import refuse_credential_shapes, sanitize_and_check_secrets
from ai_flywheel.core.time_utils import compute_valid_until, shanghai_today_ymd
from ai_flywheel.storage.db import FlywheelDB, build_knowledge_search_text
from ai_flywheel.storage.fs_store import file_sha256

# D4/Batch-2 (2026-11-23): default search surface = formal_active partitions
# minus the derived `queries` partition, matching the audit's P-2 spec.
FTS_PARTITION_IDS = {"entities", "topics", "sources", "comparisons", "synthesis", "projects"}
EXCLUDED_STATUSES = {"archived", "deprecated", "retired", "snapshot", "placeholder", "process_data"}

# How many times a domain increment is merged when the page keeps changing under it. A page
# edited by a person, an editor that rewrites files, or another Flywheel process changes
# every now and then, not several times within one merge.
MERGE_ATTEMPTS = 3

# One lock per target page, for the threads of this process. Keyed by the page's resolved path.
_PAGE_LOCKS: dict[str, Any] = {}
_PAGE_LOCKS_GUARD = threading.Lock()


class KnowledgeWriteError(ValueError):
    """Raised when a knowledge write violates allowed prefixes, path traversal, or read-back checks."""


class _PageChangedUnderMerge(Exception):
    """The page on disk is no longer the page a merge was computed on.

    Internal to the write path: caught by the merge loop, which merges again on the page
    that is there now. Nothing has been written when it is raised.
    """


def _page_write_lock(target: Path) -> Any:
    """The lock of one target page, shared by every thread of this process.

    A merge reads a page, computes, and replaces it. Two merges of the same page running
    at once would both read the first page and the later one would overwrite the other's
    result, so they take turns.
    """
    key = os.path.normcase(str(target))
    with _PAGE_LOCKS_GUARD:
        return _PAGE_LOCKS.setdefault(key, threading.RLock())


def _read_page_base(target: Path) -> tuple[str, str] | None:
    """The page as it is on disk — its text, and the SHA-256 of the bytes read — from one read.

    ``None`` when there is no page. The text is read exactly as the Wiki gate reads a
    domain page (UTF-8, undecodable bytes replaced, line ends normalised). The digest is
    taken over the same bytes, so it names the page the text came from and no other:
    read in two steps, an edit landing between them would give a digest that does not
    belong to the text, and a merge computed on the text would then pass as current.
    """
    try:
        raw = target.read_bytes() if target.is_file() else None
    except OSError:
        return None
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, hashlib.sha256(raw).hexdigest()


def _on_disk_sha256(target: Path) -> str:
    """The SHA-256 of the page's bytes right now; ``ABSENT`` when there is no page."""
    if not target.exists():
        return "ABSENT"
    return hashlib.sha256(target.read_bytes()).hexdigest()


class KnowledgeSinkAdapter:
    """Decoupled knowledge storage adapter supporting built-in KB or mounted local Markdown Wiki."""

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db
        self.wiki_gate = WikiGate(cfg)
        self._schema_loader: WikiSchemaLoader | None = None

    @property
    def root(self) -> Path:
        root = self.cfg.knowledge_root_path
        root.mkdir(parents=True, exist_ok=True)
        return root

    @property
    def schema_path(self) -> Path:
        return self.root / SCHEMA_FILE_NAME

    @property
    def schema_loader(self) -> WikiSchemaLoader:
        """The contract reader for the current root; the root follows the configuration."""
        loader = self._schema_loader
        if loader is None or loader.root != self.root:
            loader = WikiSchemaLoader(self.root)
            self._schema_loader = loader
        return loader

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
        try:
            return self.schema_loader.load(required_blocks=("partition_contract", "content_contract"))
        except WikiGateError as exc:
            raise KnowledgeWriteError(str(exc)) from None

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
            segment in FORBIDDEN_PATH_SEGMENTS or segment.startswith("archive-")
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
        projects_root_rel = ""
        if schema is not None:
            alias_map = (schema["content_contract"].get("legacy_type_aliases") or {})
            # The project partition is declared by the contract, never hardcoded.
            projects_root_rel = self.schema_loader.projects_root_rel(schema)
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
            if not project_id and projects_root_rel and rel.startswith(projects_root_rel + "/"):
                root_parts = projects_root_rel.split("/")
                parts = rel.split("/")
                if len(parts) > len(root_parts):
                    project_id = parts[len(root_parts)]
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
        # Read guard: the excluded roots (raw / archive / process memory), the schema
        # contract itself, and — on a schema-governed Wiki — every page the schema keeps
        # out of the default surface (private_sensitive / contains_credentials) stay unreadable.
        if self._is_excluded_path(norm):
            return None
        if norm == SCHEMA_FILE_NAME or target == self.schema_path.resolve():
            return None
        content = target.read_text(encoding="utf-8", errors="ignore")
        if self.schema_path.is_file() and self._parse_page(content) is None:
            return None
        return {
            "doc_path": norm,
            "content": content,
            "sha256": file_sha256(target),
        }

    # ---------------- write path ----------------

    def write_document_verified(
        self,
        relative_path: str,
        content: str,
        action: str = "create",
        *,
        sync_fts: bool = True,
        sync_practice_index: bool = False,
        approved_side_effects: list[dict[str, object]] | None = None,
        merge_increment: bool = False,
    ) -> dict[str, Any]:
        """Controlled atomic write with privacy/secrets gate, pre-write schema gate, and post-write verification.

        ``sync_fts=False`` skips the search-index rebuild: a batch that writes several
        documents rebuilds the index once at the end instead of once per document.

        ``sync_practice_index`` is enabled by P04 for schema-governed project practice
        cards. The card and its project notes-index entry are committed as one
        compensating transaction, with both paths included in postflight.

        ``merge_increment`` is for a project-domain page: ``content`` is then only what
        the session verified, and it is merged into the page as it is on disk at this
        moment, never written over it. The merged page goes through the same gates as
        any other document. An increment the page already holds writes nothing and
        returns a receipt marked ``UPDATE_NO_NEW_CONTENT``.

        The merge reads, computes and replaces under one lock per target page, and it
        replaces only the page it read: when the page on disk turns out to have changed
        in between — a person's edit, an editor that rewrites files, another process —
        nothing is written, and the increment is merged again into the page that is
        there now, gates included. A page that keeps changing is left as it is and the
        write fails; it is never overwritten with a merge of an older page.
        """
        target = self._safe_resolve(relative_path)
        rel = relative_path.replace("\\", "/").lstrip("/")
        if merge_increment:
            with _page_write_lock(target):
                return self._write_domain_increment(
                    target=target,
                    rel=rel,
                    increment=content,
                    action=action,
                    sync_fts=sync_fts,
                    sync_practice_index=sync_practice_index,
                    approved_side_effects=approved_side_effects,
                )
        return self._commit_document(
            target=target,
            rel=rel,
            content=self._prepare_document(rel, content),
            action=action,
            sync_fts=sync_fts,
            sync_practice_index=sync_practice_index,
            approved_side_effects=approved_side_effects,
        )

    @staticmethod
    def _prepare_document(rel: str, content: str) -> str:
        """The document as it is written: private spans redacted, no credential, LF line ends."""
        # v3 write-time privacy & secret leak gate (<private> redaction + refuseSecrets)
        content = sanitize_and_check_secrets(content, context=f"knowledge_doc:{rel}")
        # LF-only documents: a Windows write must never introduce CRLF into an
        # LF-governed Wiki, so the line endings are normalised before the gate sees it.
        return content.replace("\r\n", "\n")

    def _write_domain_increment(
        self,
        *,
        target: Path,
        rel: str,
        increment: str,
        action: str,
        sync_fts: bool,
        sync_practice_index: bool,
        approved_side_effects: list[dict[str, object]] | None,
    ) -> dict[str, Any]:
        """Merge ``increment`` into the page at ``target`` and replace the page. The page lock is held.

        Each attempt reads the page once (text and digest together), merges on that text,
        and commits against that digest. A page that changed after the read is not written:
        the attempt starts over on the page that is there now.
        """
        for attempt in range(1, MERGE_ATTEMPTS + 1):
            base = _read_page_base(target)
            if base is None:
                raise KnowledgeWriteError(f"Domain update target is missing, nothing to merge into: {rel}")
            content, merge_receipt = self._merge_domain_increment(rel, increment, existing=base[0])
            if merge_receipt is None:
                return {
                    "relative_path": rel,
                    "action": "update",
                    "status": UPDATE_NO_NEW_CONTENT,
                    "skipped": True,
                    "written": False,
                    "reason": "增量内容均已存在于目标页，没有新增实质内容",
                }
            try:
                receipt = self._commit_document(
                    target=target,
                    rel=rel,
                    content=self._prepare_document(rel, content),
                    action=action,
                    sync_fts=sync_fts,
                    sync_practice_index=sync_practice_index,
                    approved_side_effects=approved_side_effects,
                    expected_preimage_sha256=base[1],
                )
            except _PageChangedUnderMerge:
                continue
            if attempt > 1:
                merge_receipt = {**merge_receipt, "remerged": attempt - 1}
            receipt["merge"] = merge_receipt
            return receipt
        raise KnowledgeWriteError(
            f"Domain page kept changing under the merge, nothing was written and the page was "
            f"left as it is: {rel}"
        )

    def _commit_document(
        self,
        *,
        target: Path,
        rel: str,
        content: str,
        action: str,
        sync_fts: bool,
        sync_practice_index: bool,
        approved_side_effects: list[dict[str, object]] | None,
        expected_preimage_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Write a prepared document: the practice-index transaction, then the document itself."""
        registration = (
            self._plan_practice_index_registration(rel, content)
            if sync_practice_index
            else None
        )
        self._require_approved_practice_index(registration, approved_side_effects)
        receipt: dict[str, Any]
        if registration is not None:
            index_target = self._safe_resolve(registration.target_relative_path)
            with practice_index_write_lock(index_target):
                # Re-read under the lock: another process may have changed the index
                # between the approved preview and this write.
                registration = self._plan_practice_index_registration(rel, content)
                self._require_approved_practice_index(registration, approved_side_effects)
                receipt = self._write_document_transaction(
                    target=target,
                    rel=rel,
                    content=content,
                    action=action,
                    sync_fts=sync_fts,
                    registration=registration,
                    expected_preimage_sha256=expected_preimage_sha256,
                )
        else:
            receipt = self._write_document_transaction(
                target=target,
                rel=rel,
                content=content,
                action=action,
                sync_fts=sync_fts,
                registration=registration,
                expected_preimage_sha256=expected_preimage_sha256,
            )
        return receipt

    def _merge_domain_increment(
        self, rel: str, increment: str, *, existing: str | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        """Merge an increment into the project-domain page at ``rel`` as it is on disk.

        ``existing`` is the page as the caller read it; without it the page is read here.
        Returns the merged page and what the merge did, or ``(page, None)`` when the page
        already holds everything the increment says. Raises when the increment cannot
        enter the page without loss, or when it carries a credential: a configuration
        page is read by the strict privacy check here as it was at planning time.
        """
        if existing is None:
            existing = self.wiki_gate.read_domain_page(rel)
        if existing is None:
            raise KnowledgeWriteError(f"Domain update target is missing, nothing to merge into: {rel}")
        template = self.wiki_gate.domain_page_template(rel, existing)
        if template is None:
            raise KnowledgeWriteError(f"Target is not a project-domain page the Wiki contract declares: {rel}")
        if self.wiki_gate.is_sensitive_page(rel, increment, existing_text=existing):
            refuse_credential_shapes(increment, context=f"knowledge_doc:{rel}")
        merge = merge_domain_increment(
            existing,
            increment,
            template_sections=[str(name) for name in (template.get("body_sections") or [])],
            today=shanghai_today_ymd(),
        )
        if not merge.ok:
            raise KnowledgeWriteError(
                f"Domain increment cannot be merged without loss into {rel}: {'；'.join(merge.problems)}"
            )
        if not merge.has_additions:
            return existing, None
        if not preserves_existing(existing, merge.merged):
            # The merge is additive by construction. A result that dropped a line of the
            # page is a defect, and it never reaches the file.
            raise KnowledgeWriteError(f"Domain merge would not preserve the existing page: {rel}")
        return merge.merged, {
            "incremental": True,
            "existing_lines": len(existing.splitlines()),
            "added_lines": merge.added,
            "duplicates_removed": merge.duplicates,
            "placeholders_replaced": list(merge.placeholders),
            "existing_preserved": True,
        }

    def _write_document_transaction(
        self,
        *,
        target: Path,
        rel: str,
        content: str,
        action: str,
        sync_fts: bool,
        registration: PracticeIndexRegistration | None,
        expected_preimage_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Write one document and verify it. Raises `_PageChangedUnderMerge` before anything
        is written when ``expected_preimage_sha256`` is given and the page is no longer the
        page ``content`` was computed from."""
        gate_result = self.wiki_gate.validate_document(rel, content)
        if gate_result.applicable and not gate_result.ok:
            self.wiki_gate.write_artifact(
                gate_result,
                rel,
                {
                    "stage": "pre_write",
                    "practice_index": registration.side_effect() if registration else None,
                },
            )
            raise WikiGateError(_format_gate_failure(rel, gate_result))

        preimage_bytes = target.read_bytes() if target.exists() else None
        preimage_sha = (
            hashlib.sha256(preimage_bytes).hexdigest()
            if preimage_bytes is not None
            else "ABSENT"
        )
        if action == "update" and _is_unchanged(preimage_bytes, content):
            raise KnowledgeWriteError(f"Update action produced no content change on {rel}")
        if expected_preimage_sha256 is not None and preimage_sha != expected_preimage_sha256:
            # A merge of a page that is no longer on disk: written now, it would overwrite
            # whatever changed the page. Nothing has been written.
            raise _PageChangedUnderMerge(rel)

        index_target: Path | None = None
        index_preimage: bytes | None = None
        index_written = False
        if registration is not None and registration.status == "update":
            index_target = self._safe_resolve(registration.target_relative_path)
            index_gate = self.wiki_gate.validate_document(
                registration.target_relative_path,
                registration.new_content,
            )
            if index_gate.applicable and not index_gate.ok:
                self.wiki_gate.write_artifact(
                    index_gate,
                    registration.target_relative_path,
                    {
                        "stage": "pre_write",
                        "practice_index": registration.side_effect(),
                        "card_relative_path": rel,
                    },
                )
                raise WikiGateError(
                    _format_gate_failure(registration.target_relative_path, index_gate)
                )
            index_preimage = index_target.read_bytes()
            try:
                _atomic_write_text(index_target, registration.new_content)
                index_written = True
            except OSError as exc:
                raise KnowledgeWriteError(
                    f"Practice index write failed for "
                    f"'{registration.target_relative_path}': {exc}"
                ) from exc

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if expected_preimage_sha256 is not None and _on_disk_sha256(target) != expected_preimage_sha256:
                # Read once more right before the replace: the index write above took time.
                # The index goes back to what it was along with this document's not being written.
                raise _PageChangedUnderMerge(rel)
            _atomic_write_text(target, content)
        except Exception:
            if index_written and index_target is not None:
                self._rollback(index_target, index_preimage)
            raise

        readback = _read_text_exact(target)
        postimage_sha = file_sha256(target)
        if readback != content:
            rollback = self._rollback(target, preimage_bytes)
            if index_written and index_target is not None:
                rollback["practice_index"] = self._rollback(index_target, index_preimage)
            raise KnowledgeWriteError(f"Read-back content mismatch on {rel}: {rollback}")
        if (
            index_target is not None
            and registration is not None
            and _read_text_exact(index_target) != registration.new_content
        ):
            self._rollback(target, preimage_bytes)
            self._rollback(index_target, index_preimage)
            raise KnowledgeWriteError(
                f"Read-back mismatch on practice index "
                f"'{registration.target_relative_path}'"
            )

        postflight_paths = [rel]
        if registration is not None:
            postflight_paths.append(registration.target_relative_path)
        rollback_info: dict[str, Any] | None = None
        if gate_result.applicable:
            try:
                self.wiki_gate.run_topic_lint(gate_result, rel)
                if gate_result.ok:
                    self.wiki_gate.run_postflight(gate_result, postflight_paths)
            except Exception as exc:
                gate_result.error(
                    "D2_FAILED",
                    f"post-write verification raised {type(exc).__name__}: {exc}",
                )
            if not gate_result.ok:
                rollback_info = self._rollback(target, preimage_bytes)
                if index_written and index_target is not None:
                    rollback_info["practice_index"] = self._rollback(
                        index_target,
                        index_preimage,
                    )
                try:
                    rollback_info["resync"] = self.wiki_gate.resync_after_rollback(
                        postflight_paths
                    )
                except Exception as exc:
                    rollback_info["resync"] = {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if sync_fts:
                    try:
                        self.sync_fts_index()
                    except Exception as exc:
                        rollback_info["fts_resync"] = {
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                self.wiki_gate.write_artifact(
                    gate_result,
                    rel,
                    {
                        "stage": "post_write",
                        "rollback": rollback_info,
                        "practice_index": registration.receipt() if registration else None,
                    },
                )
                raise WikiGateError(_format_gate_failure(rel, gate_result, rollback_info))
            self.wiki_gate.write_artifact(
                gate_result,
                rel,
                {
                    "stage": "post_write",
                    "practice_index": registration.receipt() if registration else None,
                },
            )

        if sync_fts:
            self.sync_fts_index()
        fm_parsed = self._parse_page_lenient(content).get("frontmatter") or {}
        tier = str(fm_parsed.get("freshness_tier") or "evolving").strip().lower()
        if tier not in {"volatile", "evolving", "evergreen"}:
            tier = "evolving"
        last_verified = str(fm_parsed.get("last_verified") or shanghai_today_ymd()).strip()
        valid_until = str(
            fm_parsed.get("valid_until")
            or compute_valid_until(tier, last_verified)
        ).strip()
        doc_status = str(fm_parsed.get("status") or "").strip().lower()
        initial_state = doc_status if doc_status in {"archived", "superseded"} else "fresh"
        self.db.upsert_knowledge_freshness(
            rel,
            freshness_tier=tier,
            last_verified_at=last_verified,
            valid_until=valid_until,
            freshness_state=initial_state,
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
            "practice_index": registration.receipt() if registration else None,
        }

    def _plan_practice_index_registration(
        self,
        relative_path: str,
        content: str,
    ) -> PracticeIndexRegistration | None:
        try:
            return self.wiki_gate.plan_practice_index_registration(relative_path, content)
        except PracticeIndexError as exc:
            raise KnowledgeWriteError(str(exc)) from None

    @staticmethod
    def _require_approved_practice_index(
        registration: PracticeIndexRegistration | None,
        approved_side_effects: list[dict[str, object]] | None,
    ) -> None:
        if registration is not None and not approved_registration_matches(
            registration,
            approved_side_effects,
        ):
            raise KnowledgeWriteError(
                "Practice index registration is not in the approved write scope "
                f"for '{registration.target_relative_path}'. Re-plan and approve it."
            )

    @staticmethod
    def _rollback(target: Path, preimage_bytes: bytes | None) -> dict[str, Any]:
        try:
            if preimage_bytes is None:
                target.unlink(missing_ok=True)
                state = "deleted_new_file"
            else:
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.rollback.",
                    suffix=".tmp",
                    dir=target.parent,
                )
                temp_path = Path(temp_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(preimage_bytes)
                    temp_path.replace(target)
                finally:
                    temp_path.unlink(missing_ok=True)
                state = "restored_preimage"
        except OSError as exc:
            return {"state": "rollback_failed", "error": str(exc)}
        return {"state": state}

    def sync_fts_index(self) -> int:
        docs, _skipped = self._search_documents()
        fts_docs = [
            d
            for d in docs
            if str(d.get("status") or "").strip().lower() not in {"archived", "superseded"}
        ]
        count = self.db.rebuild_knowledge_fts(fts_docs)
        existing_map = self.db.get_knowledge_freshness_map()
        synced_paths: set[str] = set()

        def _sync_freshness_entry(
            rel: str, doc_status: str, tier: str, last_v: str, valid_u: str
        ) -> None:
            synced_paths.add(rel)
            prev = existing_map.get(rel)
            if doc_status in {"archived", "superseded"}:
                if not (tier or last_v or valid_u or prev):
                    return
                if not prev or prev.get("freshness_state") != doc_status:
                    effective_tier = tier or (prev.get("freshness_tier") if prev else "") or "evolving"
                    if effective_tier not in {"volatile", "evolving", "evergreen"}:
                        effective_tier = "evolving"
                    effective_v = last_v or (prev.get("last_verified_at") if prev else "") or shanghai_today_ymd()
                    effective_u = (
                        valid_u
                        or (prev.get("valid_until") if prev else "")
                        or compute_valid_until(effective_tier, effective_v)
                    )
                    self.db.upsert_knowledge_freshness(
                        rel,
                        freshness_tier=effective_tier,
                        last_verified_at=effective_v,
                        valid_until=effective_u,
                        freshness_state=doc_status,
                    )
                return

            if not (tier or last_v or valid_u):
                return

            if prev and prev.get("freshness_state") in {"archived", "superseded"}:
                if not (last_v and last_v > str(prev.get("last_verified_at") or "")):
                    return

            if prev and prev.get("last_verified_at") and last_v and str(prev["last_verified_at"]) >= last_v:
                return

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

        for doc in docs:
            _sync_freshness_entry(
                doc["doc_path"],
                str(doc.get("status") or "").strip().lower(),
                doc.get("freshness_tier") or "",
                doc.get("last_verified") or "",
                doc.get("valid_until") or "",
            )

        # In schema-governed Wikis, `_search_documents` excludes `status: archived`
        # from `knowledge_fts`; still inspect those skipped files leniently so
        # `knowledge_card_metrics` retires them cleanly without indexing them.
        for md, rel in self._iter_candidate_files():
            if rel in synced_paths:
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fm = self._parse_page_lenient(text).get("frontmatter") or {}
            doc_status = str(fm.get("status") or "").strip().lower()
            if doc_status not in {"archived", "superseded"}:
                continue
            _sync_freshness_entry(
                rel,
                doc_status,
                str(fm.get("freshness_tier") or "").strip(),
                str(fm.get("last_verified") or "").strip(),
                str(fm.get("valid_until") or "").strip(),
            )
        return count


def _is_unchanged(preimage_bytes: bytes | None, content: str) -> bool:
    """True when an update would not change the document: identical bytes, or the same
    text modulo line endings (an LF-governed Wiki rewrites CRLF as LF)."""
    if preimage_bytes is None:
        return False
    if preimage_bytes == content.encode("utf-8"):
        return True
    try:
        previous = preimage_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return previous.replace("\r\n", "\n") == content


def _atomic_write_text(target: Path, content: str) -> None:
    """Write UTF-8/LF through a unique sibling temp file and atomically replace."""
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        temp_path.replace(target)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_text_exact(target: Path) -> str:
    with target.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


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
