"""The distill-queue state machine: four ingress events, three gates, one trail.

Every way a session can enter the queue converges here — Codex archive
reconciliation, the Claude Code ``SessionEnd`` hook, an MCP declaration, and a
manual portal submission — so the queue behaves identically whatever the entry
point. Each transition writes the structured ``audit_events`` trail
(``entity_type="queue_item"`` with ``from_status`` / ``to_status`` /
``reason_code``) and publishes ``queue_updated`` on the event broker.

Deterministic transcript compaction and chunking live in
:mod:`ai_flywheel.core.queue_compaction`; batch archive reconciliation and
re-triage sweeps live in :mod:`ai_flywheel.core.queue_reconcile`.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_flywheel.core.privacy import (
    BRIEF_REDACTED,
    BRIEF_TRUNCATED,
    MAX_BRIEF_DEPTH,
    MAX_BRIEF_ITEMS,
    MAX_BRIEF_VALUE_CHARS,
    key_names_credential as _key_names_credential,
    mask_credential_shapes,
    redact_structured_brief,
)
from ai_flywheel.core.queue_compaction import (
    COMPACT_HEAD_CHARS,
    COMPACT_TAIL_CHARS,
    DISTILL_MODES,
    DISTILL_MODE_FULL,
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_EVENT,
    HAND_DISTILLED_META_KEYS,
    HAND_DISTILLED_SKIP,
    MAX_CHUNKS_PER_SESSION,
    MAX_COMPACT_TURN_CHARS,
    MAX_FULL_TRANSCRIPT_CHARS,
    MAX_USER_TURN_CHARS,
    USER_COMPACT_HEAD_CHARS,
    USER_COMPACT_TAIL_CHARS,
    _cap_chunks_preserving_head_and_tail,
    _path_is_within,
    _safe_mtime,
    _scan_source_fields,
    compact_turn_text,
    compute_content_hash,
    prepare_queue_chunks,
    read_full_transcript,
    redact_hand_distilled_paths,
    render_structured_brief,
    split_dialogue_turns,
)
from ai_flywheel.core.queue_reconcile import (
    MAX_ARCHIVE_FILES_PER_SOURCE,
    MAX_OPENING_CHARS,
    QUIET_WINDOW_ACTIVE,
    RE_TRIAGE_SKIP_REASON_CODES,
    QueueReconcileMixin,
)
from ai_flywheel.core.time_utils import SHANGHAI_TZ, shanghai_now_iso
from ai_flywheel.core.triage import (
    _UUID_RE,
    derive_readable_title,
    extract_jsonl_metadata,
    has_explicit_opt_out,
    is_antigravity_client,
    map_triage_reason_code,
    scan_quiet_window_seconds,
    transcript_is_active,
    triage_session,
)
from ai_flywheel.models import (
    DistillQueueItem,
    IngressChannel,
    QueueStatus,
    RetroStatus,
    SessionRecord,
    SessionStatus,
)
from ai_flywheel.storage.fs_store import redact_text

if TYPE_CHECKING:
    from ai_flywheel.adapters.sessions import SessionAdapter
    from ai_flywheel.config import FlywheelConfig
    from ai_flywheel.core.pipeline.events import EventBroker
    from ai_flywheel.storage.db import FlywheelDB

logger = logging.getLogger("ai_flywheel.queue")


def _declaration_meta(mode: str, paths: list[str], *, declared_at: str = "") -> dict[str, Any]:
    """The session-metadata mirror of a hand-distilled declaration."""
    meta: dict[str, Any] = {
        "distill_mode": mode,
        "hand_distilled_paths": list(paths),
        "hand_distilled_at": declared_at or shanghai_now_iso(),
    }
    if mode == DISTILL_MODE_SKIPPED:
        meta["hand_distilled_skip"] = True
    return meta



# ---------------------------------------------------------------- the service


class DistillQueueService(QueueReconcileMixin):
    """One behaviour source for every ``distill_queue`` transition."""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        sessions: SessionAdapter,
        broker: EventBroker | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.sessions = sessions
        self.broker = broker

    # ---------------- shared helpers ----------------

    @property
    def max_item_chars(self) -> int:
        return max(1000, int(self.cfg.sessions.queue_max_item_chars))

    @property
    def max_retries(self) -> int:
        return max(0, int(self.cfg.sessions.queue_max_retries))

    def _transcript_is_being_written(
        self, path: Path | None, client: str, *, now_ts: float | None
    ) -> bool:
        """Whether ``path`` is an Antigravity transcript written too recently to archive.

        Only the Antigravity client has no archive event to wait for; for every other
        client this is always ``False``, and so it is when the quiet window is off.
        """
        if path is None or not is_antigravity_client(client):
            return False
        quiet_seconds = scan_quiet_window_seconds(self.cfg)
        return transcript_is_active(_safe_mtime(path), quiet_seconds=quiet_seconds, now_ts=now_ts)

    def _notify(
        self,
        item: DistillQueueItem,
        *,
        from_status: str,
        to_status: str,
        reason_code: str,
        actor: str,
    ) -> None:
        if self.broker is None:
            return
        try:
            self.broker.publish(
                "queue_updated",
                {
                    "queue_id": item.queue_id,
                    "session_key": item.session_key,
                    "client": item.client,
                    "revision": item.revision,
                    "chunk_index": item.chunk_index,
                    "chunk_total": item.chunk_total,
                    "from_status": from_status,
                    "to_status": to_status,
                    "reason_code": reason_code,
                    "actor": actor,
                },
            )
        except Exception:  # pragma: no cover - a broken subscriber never breaks the queue
            logger.debug("queue_updated publish failed", exc_info=True)

    def _record_created(
        self,
        item: DistillQueueItem,
        *,
        actor: str,
        reason_code: str = "",
        summary: str = "",
    ) -> None:
        self.db.append_audit_event(
            entity_type="queue_item",
            entity_id=item.queue_id,
            event_type="queue_item_created",
            actor=actor,
            summary=summary or f"created {item.status.value} item",
            payload={
                "session_key": item.session_key,
                "status": item.status.value,
                "revision": item.revision,
                "chunk_index": item.chunk_index,
                "chunk_total": item.chunk_total,
                "ingress_channel": item.ingress_channel.value,
                **self._declared_fields(item),
            },
            to_status=item.status.value,
            reason_code=reason_code,
        )

    @staticmethod
    def _declared_fields(item: DistillQueueItem) -> dict[str, Any]:
        """The hand-distilled declaration an item carries, for its audit trail (empty otherwise)."""
        payload = item.payload if isinstance(item.payload, dict) else {}
        fields: dict[str, Any] = {}
        mode = str(payload.get("distill_mode") or "")
        if mode in DISTILL_MODES:
            fields["distill_mode"] = mode
        if isinstance(payload.get("hand_distilled_paths"), list):
            fields["hand_distilled_paths"] = list(payload["hand_distilled_paths"])
        return fields

    def _transition(
        self,
        item: DistillQueueItem,
        status: QueueStatus,
        *,
        reason_code: str = "",
        actor: str = "system",
        summary: str = "",
        detail: str = "",
        reset_retry: bool = False,
    ) -> DistillQueueItem | None:
        from_status = item.status.value
        updated = self.db.update_queue_item_status(
            item.queue_id,
            status,
            exclude_reason_code=reason_code if status is QueueStatus.EXCLUDED else None,
            exclude_reason_detail=detail if status is QueueStatus.EXCLUDED else None,
            reset_retry=reset_retry,
            actor=actor,
            reason_code=reason_code,
            summary=summary,
        )
        if updated is not None:
            self._notify(
                updated,
                from_status=from_status,
                to_status=status.value,
                reason_code=reason_code,
                actor=actor,
            )
        return updated

    def _insert_items(
        self,
        items: list[DistillQueueItem],
        *,
        actor: str,
        reason_code: str = "",
        summary: str = "",
    ) -> list[DistillQueueItem]:
        saved: list[DistillQueueItem] = []
        for item in items:
            stored = self.db.upsert_queue_item(item)
            self._record_created(stored, actor=actor, reason_code=reason_code, summary=summary)
            self._notify(
                stored,
                from_status="",
                to_status=stored.status.value,
                reason_code=reason_code,
                actor=actor,
            )
            saved.append(stored)
        return saved

    def _supersede_queued_items(self, session_key: str, *, actor: str) -> list[DistillQueueItem]:
        superseded: list[DistillQueueItem] = []
        for item in self.db.list_session_queue_items(session_key, statuses=[QueueStatus.QUEUED]):
            updated = self._transition(
                item,
                QueueStatus.EXCLUDED,
                reason_code="SUPERSEDED",
                actor=actor,
                summary="superseded by a newer revision",
            )
            if updated is not None:
                superseded.append(updated)
        return superseded

    def _session_text(self, record: SessionRecord) -> str:
        """The full session text (up to 500k), never the 24k review excerpt."""
        parts: list[str] = []
        if record.title:
            parts.append(f"Title: {record.title}")
        if record.summary:
            parts.append(f"Summary: {redact_text(record.summary)}")
        if record.content and record.content.strip():
            parts.append(redact_text(record.content))
        elif record.transcript_path:
            excerpt = read_full_transcript(Path(record.transcript_path))
            if excerpt:
                parts.append(excerpt)
        text = "\n\n".join(part for part in parts if part)
        if not text.strip() and record.transcript_path:
            text = self.sessions.load_session_text(record, max_chars=MAX_FULL_TRANSCRIPT_CHARS)
        return text[:MAX_FULL_TRANSCRIPT_CHARS]

    def _build_item(
        self,
        record: SessionRecord,
        *,
        revision: int,
        chunk: dict[str, Any],
        chunk_index: int,
        chunk_total: int,
        channel: IngressChannel,
        status: QueueStatus,
        priority: str,
        content_hash: str,
        total_chars: int,
        compacted_chars: int,
        from_turn: int,
        to_turn: int,
        focus_note: str = "",
        reason_code: str = "",
        reason_detail: str = "",
        source_path: str | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> DistillQueueItem:
        payload: dict[str, Any] = {
            "chunk_text": chunk.get("chunk_text", ""),
            "turn_start": int(chunk.get("turn_start", 0)),
            "turn_end": int(chunk.get("turn_end", 0)),
            "total_turns": int(to_turn),
            "consumed_from_turn": int(from_turn),
            "consumed_to_turn": int(to_turn),
            "ingress_channel": channel.value,
        }
        if reason_code:
            payload["exclude_reason_code"] = reason_code
        rec_meta = record.metadata if isinstance(record.metadata, dict) else {}
        parent_session_key = str(rec_meta.get("parent_session_key") or "")
        is_subagent = bool(rec_meta.get("is_subagent", False))
        if parent_session_key:
            payload["parent_session_key"] = parent_session_key
        if is_subagent:
            payload["is_subagent"] = True
        if rec_meta.get("subagent_nickname"):
            payload["subagent_nickname"] = rec_meta["subagent_nickname"]
        if rec_meta.get("subagent_role"):
            payload["subagent_role"] = rec_meta["subagent_role"]
        payload.update(payload_extra or {})
        return DistillQueueItem(
            queue_id=uuid.uuid4().hex,
            session_key=record.session_key,
            parent_session_key=parent_session_key,
            is_subagent=is_subagent,
            client=record.client,
            project=record.project or "generic",
            workspace=record.workspace or "",
            title=record.title or "",
            revision=revision,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            ingress_channel=channel,
            status=status,
            priority=priority or "P1_GENERAL",
            exclude_reason_code=reason_code,
            exclude_reason_detail=reason_detail,
            focus_note=focus_note,
            content_hash=content_hash,
            total_chars=int(total_chars),
            compacted_chars=int(compacted_chars),
            is_oversized=chunk_total > 1,
            consumed_from_turn=int(from_turn),
            consumed_to_turn=int(to_turn),
            max_retries=self.max_retries,
            source_path=source_path if source_path is not None else record.transcript_path,
            payload=payload,
        )

    def _build_chunk_items(
        self,
        record: SessionRecord,
        prepared: dict[str, Any],
        *,
        revision: int,
        channel: IngressChannel,
        status: QueueStatus,
        priority: str,
        content_hash: str,
        focus_note: str = "",
        reason_code: str = "",
        reason_detail: str = "",
        source_path: str | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> list[DistillQueueItem]:
        from_turn = int(prepared.get("from_turn", 0))
        to_turn = int(prepared.get("total_turns", 0))
        return [
            self._build_item(
                record,
                revision=revision,
                chunk=chunk,
                chunk_index=int(chunk["chunk_index"]),
                chunk_total=int(chunk["chunk_total"]),
                channel=channel,
                status=status,
                priority=priority,
                content_hash=content_hash,
                total_chars=int(prepared.get("total_chars", 0)),
                compacted_chars=int(prepared.get("compacted_chars", 0)),
                from_turn=from_turn,
                to_turn=to_turn,
                focus_note=focus_note,
                reason_code=reason_code,
                reason_detail=reason_detail,
                source_path=source_path,
                payload_extra=payload_extra,
            )
            for chunk in prepared["chunks"]
        ]

    def _ensure_session(self, session_key: str) -> SessionRecord:
        existing = self.db.get_session(session_key)
        if existing is not None:
            return existing
        if ":" in session_key:
            client, _, session_id = session_key.partition(":")
        else:
            client, session_id = "generic", session_key
        client = client or "generic"
        session_id = session_id or session_key
        record = SessionRecord(
            session_key=session_key,
            client=client,
            session_id=session_id,
            title=f"{client} · {session_id[:16]}",
            status=SessionStatus.PENDING,
        )
        return self.db.upsert_session(record)

    @staticmethod
    def _item_brief(item: DistillQueueItem) -> dict[str, Any]:
        return {
            "queue_id": item.queue_id,
            "session_key": item.session_key,
            "revision": item.revision,
            "chunk_index": item.chunk_index,
            "chunk_total": item.chunk_total,
            "status": item.status.value,
            "priority": item.priority,
            "ingress_channel": item.ingress_channel.value,
            "exclude_reason_code": item.exclude_reason_code,
        }

    # ---------------- ingress 1: explicit enqueue ----------------

    def on_explicit_enqueue(
        self,
        session: SessionRecord | None = None,
        *,
        client: str = "",
        session_id: str = "",
        title: str = "",
        project: str = "generic",
        workspace: str = "",
        transcript_path: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        focus_note: str = "",
        channel: IngressChannel = IngressChannel.MCP_EXPLICIT,
        actor: str = "mcp",
        force: bool = True,
        priority: str = "P0_CORE",
        structured_brief: dict[str, Any] | None = None,
        distill_mode: str = "",
        hand_distilled_paths: list[str] | None = None,
    ) -> list[DistillQueueItem]:
        """A user/MCP declaration: bypass the noise filter and queue at P0.

        ``force`` (the default) clears an earlier ``opt_out`` because the user is
        now explicitly asking for this session to be distilled. Without it, an
        opted-out session is left untouched and nothing is enqueued. It lifts a
        hand-distilled declaration the same way — an explicit request distills the
        session in full again — while a caller without ``force`` inherits whatever
        the session was declared, and never re-opens a session taken out by hand.

        ``structured_brief`` is the client's structured evidence for the review:
        redacted, kept on the item and counted into the content fingerprint, so the
        same transcript with a new brief is a new revision and the same brief twice
        is the same submission.

        ``distill_mode="governance_only"`` with ``hand_distilled_paths`` is the
        ``hand_distilled`` declaration: the client already wrote the knowledge up,
        so the session is queued for its rules, mistakes and dissents only.
        """
        if session is not None:
            record = session.model_copy(deep=True)
        else:
            client_name = (client or "generic").strip() or "generic"
            sid = (session_id or "").strip()
            if not sid:
                return []
            existing = self.db.get_session(f"{client_name}:{sid}")
            base = existing.model_dump() if existing is not None else {}
            record = SessionRecord(
                session_key=f"{client_name}:{sid}",
                client=client_name,
                session_id=sid,
                title=title or base.get("title") or f"{client_name} · {sid[:16]}",
                project=(
                    project
                    if project and project.strip().lower() not in {"", "generic"}
                    else (base.get("project") or "generic")
                ),
                workspace=workspace or base.get("workspace") or "",
                transcript_path=transcript_path or base.get("transcript_path"),
                summary=summary if summary is not None else base.get("summary"),
                content=content if content is not None else base.get("content"),
                tags=list(dict.fromkeys([*(tags or []), *(base.get("tags") or [])])),
                metadata=dict(base.get("metadata") or {}),
                priority=priority,
                status=SessionStatus.PENDING,
            )
        meta = dict(record.metadata) if isinstance(record.metadata, dict) else {}
        declared = self._hand_distilled_declaration(record.session_key, record)
        declared_mode = declared["distill_mode"] if declared is not None else DISTILL_MODE_FULL
        declared_paths = declared["hand_distilled_paths"] if declared is not None else []
        if record.opt_out and not force:
            return []
        # A session the client took out by hand is never re-opened by a passive caller.
        if not force and declared_mode == DISTILL_MODE_SKIPPED:
            return []
        mode = str(distill_mode or "").strip().lower()
        if mode and mode != DISTILL_MODE_GOVERNANCE_ONLY:
            raise ValueError(f"unsupported distill_mode: {distill_mode}")
        if not mode:
            mode = DISTILL_MODE_FULL if force else declared_mode
        paths = redact_hand_distilled_paths(hand_distilled_paths)
        if mode == DISTILL_MODE_GOVERNANCE_ONLY:
            # Every document reported for this session stays reported: a second
            # write-up adds a path, it never un-reports the first one. Only an
            # explicit enqueue, which lifts the declaration, starts the list over.
            paths = list(dict.fromkeys([*paths, *declared_paths]))
        brief = redact_structured_brief(structured_brief)

        # The session metadata mirrors the declaration; an explicit full request clears it.
        if mode == DISTILL_MODE_GOVERNANCE_ONLY:
            meta.update(
                {
                    **_declaration_meta(mode, paths, declared_at=str(meta.get("hand_distilled_at") or "")),
                    "explicit_ingest": True,
                }
            )
        elif force:
            for key in HAND_DISTILLED_META_KEYS:
                meta.pop(key, None)
        record.metadata = meta
        record.opt_out = False
        record.opt_out_reason = ""
        record.status = SessionStatus.PENDING
        stored = self.db.upsert_session(record)

        text = self._session_text(stored)
        content_hash = compute_content_hash(text, structured_brief=brief or None)
        total_turns = len(split_dialogue_turns(text))
        # Same content, same brief and same mode already queued: an explicit re-submission
        # is idempotent. A declaration that changes the mode replaces the queued revision.
        active = self.db.list_session_queue_items(stored.session_key, statuses=[QueueStatus.QUEUED])
        if (
            active
            and len(active) == active[0].chunk_total
            and all(
                item.content_hash == content_hash and self._item_distill_mode(item) == mode
                for item in active
            )
        ):
            active = sorted(active, key=lambda item: item.chunk_index)
            if mode == DISTILL_MODE_GOVERNANCE_ONLY:
                return self._merge_declared_paths(
                    active, paths, mode=DISTILL_MODE_GOVERNANCE_ONLY, actor=actor
                )
            return active

        superseded = self._supersede_queued_items(stored.session_key, actor=actor)
        revision = self.db.get_latest_queue_revision(stored.session_key) + 1
        prepared = prepare_queue_chunks(text, from_turn=0, max_item_chars=self.max_item_chars)
        governance = mode == DISTILL_MODE_GOVERNANCE_ONLY
        items = self._build_chunk_items(
            stored,
            prepared,
            revision=revision,
            channel=channel,
            status=QueueStatus.QUEUED,
            priority=priority,
            content_hash=content_hash,
            focus_note=focus_note,
            payload_extra=self._declaration_payload(mode, paths, brief, text),
        )
        saved = self._insert_items(
            items,
            actor=actor,
            summary=(
                f"hand_distilled: governance-only enqueue via {channel.value}"
                if governance
                else f"explicit enqueue via {channel.value}"
            ),
        )
        self.db.update_session_watermark(
            stored.session_key,
            latest_content_hash=content_hash,
            latest_turn_count=total_turns,
        )
        if governance:
            self._audit_hand_distilled(
                [*saved, *superseded],
                mode=mode,
                paths=paths,
                actor=actor,
                declaration="governance_only",
                summary="hand_distilled: session queued for governance only",
            )
        return saved

    # ---------------- distill mode (hand_distilled) ----------------

    @staticmethod
    def _item_distill_mode(item: DistillQueueItem) -> str:
        """The mode a queue item was enqueued under: ``full`` unless it declares another."""
        payload = item.payload if isinstance(item.payload, dict) else {}
        mode = str(payload.get("distill_mode") or "")
        return mode if mode in DISTILL_MODES else DISTILL_MODE_FULL

    @staticmethod
    def _item_paths(item: DistillQueueItem) -> list[str]:
        payload = item.payload if isinstance(item.payload, dict) else {}
        return [str(path) for path in payload.get("hand_distilled_paths") or []]

    def distill_mode_of(self, session: SessionRecord) -> str:
        """The distill mode a session is under right now (``full`` unless declared otherwise)."""
        declared = self._hand_distilled_declaration(session.session_key, session)
        return declared["distill_mode"] if declared is not None else DISTILL_MODE_FULL

    def _hand_distilled_declaration(
        self, session_key: str, session: SessionRecord | None = None
    ) -> dict[str, Any] | None:
        """The hand-distilled declaration a session is under, or ``None`` for a plain session.

        The queue history is the record that survives: a later registration can replace
        the session metadata wholesale, but nothing rewrites a queue item. The newest
        revision that declares a mode wins, so an explicit ``enqueue`` (a ``full`` item)
        lifts an earlier declaration exactly like it clears an ``opt_out``.
        """
        meta = session.metadata if session is not None and isinstance(session.metadata, dict) else {}
        paths = [str(path) for path in meta.get("hand_distilled_paths") or [] if str(path).strip()]
        mode = ""
        for item in self.db.list_session_queue_items(session_key):
            payload = item.payload if isinstance(item.payload, dict) else {}
            item_mode = str(payload.get("distill_mode") or "")
            if item_mode in DISTILL_MODES:
                mode = item_mode
                if "hand_distilled_paths" in payload:
                    paths = [str(path) for path in payload.get("hand_distilled_paths") or []]
                break
        if not mode:
            declared = str(meta.get("distill_mode") or "")
            if declared not in DISTILL_MODES and meta.get("hand_distilled_skip") is True:
                declared = DISTILL_MODE_SKIPPED
            mode = declared if declared in DISTILL_MODES else ""
        if mode not in {DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}:
            return None
        return {"distill_mode": mode, "hand_distilled_paths": paths}

    @staticmethod
    def _declaration_payload(
        mode: str, paths: list[str], brief: dict[str, Any], text: str
    ) -> dict[str, Any]:
        """What a queue item carries beyond its chunk: its distill mode and evidence."""
        extra: dict[str, Any] = {"distill_mode": mode}
        if mode == DISTILL_MODE_GOVERNANCE_ONLY:
            extra["hand_distilled_paths"] = list(paths)
        if brief:
            extra["structured_brief"] = brief
            # The transcript behind the brief, for the archive gate: an archive event
            # arrives without the brief and must still recognise the same conversation.
            extra["text_hash"] = compute_content_hash(text)
        return extra

    def _audit_hand_distilled(
        self,
        items: list[DistillQueueItem],
        *,
        mode: str,
        paths: list[str],
        actor: str,
        declaration: str,
        summary: str,
    ) -> None:
        """One ``queue_item`` audit event per item a ``hand_distilled`` declaration touched."""
        for item in items:
            self.db.append_audit_event(
                entity_type="queue_item",
                entity_id=item.queue_id,
                event_type=HAND_DISTILLED_EVENT,
                actor=actor,
                summary=summary,
                payload={
                    "session_key": item.session_key,
                    "declaration": declaration,
                    "distill_mode": mode,
                    "hand_distilled_paths": list(paths),
                },
                to_status=item.status.value,
                reason_code=HAND_DISTILLED_SKIP if mode == DISTILL_MODE_SKIPPED else "",
            )

    def _merge_declared_paths(
        self, items: list[DistillQueueItem], paths: list[str], *, mode: str, actor: str
    ) -> list[DistillQueueItem]:
        """A repeated ``hand_distilled`` for a session whose newest revision already declares
        ``mode``: merge the paths reported since into those items, in place."""
        merged = list(paths)
        for item in items:
            for path in self._item_paths(item):
                if path not in merged:
                    merged.append(path)
        if all(sorted(self._item_paths(item)) == sorted(merged) for item in items):
            return items
        refreshed: list[DistillQueueItem] = []
        for item in items:
            updated = self.db.update_queue_item_metadata(
                item.queue_id, payload={**item.payload, "hand_distilled_paths": merged}
            )
            refreshed.append(updated or item)
        self._audit_hand_distilled(
            refreshed,
            mode=mode,
            paths=merged,
            actor=actor,
            declaration=(
                DISTILL_MODE_GOVERNANCE_ONLY if mode == DISTILL_MODE_GOVERNANCE_ONLY else "skip"
            ),
            summary="hand_distilled: reported knowledge paths updated",
        )
        return refreshed

    def _apply_hand_distilled_skip(
        self,
        session: SessionRecord,
        *,
        paths: list[str],
        channel: IngressChannel,
        actor: str,
        source_path: str | None = None,
        detail: str,
    ) -> dict[str, Any]:
        """Take the session out of distillation and keep the decision visible as ``excluded``.

        Like the opt-out veto, the decision stays in the inventory: the unconsumed
        items become ``excluded (HAND_DISTILLED_SKIP)``, and a session whose newest
        revision says otherwise — one that never had a queue item, or one an
        explicit ``enqueue`` took back — gets one visible record as its newest
        revision instead of disappearing without a trace.

        It is the newest revision that is asked, never an older one. A skip on
        record from before is not this skip: said again after an explicit
        ``enqueue``, it takes the session out again.
        """
        key = session.session_key
        touched: list[DistillQueueItem] = []
        for item in self.db.list_session_queue_items(key, statuses=[QueueStatus.QUEUED]):
            updated = self._transition(
                item,
                QueueStatus.EXCLUDED,
                reason_code=HAND_DISTILLED_SKIP,
                detail=detail,
                actor=actor,
                summary="hand_distilled skip",
            )
            if updated is None:
                continue
            stamped = self.db.update_queue_item_metadata(
                updated.queue_id,
                payload={
                    **updated.payload,
                    "distill_mode": DISTILL_MODE_SKIPPED,
                    "hand_distilled_paths": list(paths),
                },
            )
            touched.append(stamped or updated)

        history = self.db.list_session_queue_items(key)
        newest_revision = history[0].revision if history else 0
        visible = [item for item in history if item.revision == newest_revision]
        if not visible or any(self._item_distill_mode(item) != DISTILL_MODE_SKIPPED for item in visible):
            placeholder = self._build_item(
                session,
                revision=newest_revision + 1,
                chunk={"chunk_text": "", "turn_start": 0, "turn_end": 0},
                chunk_index=1,
                chunk_total=1,
                channel=channel,
                status=QueueStatus.EXCLUDED,
                priority=session.priority or "P1_GENERAL",
                content_hash="",
                total_chars=0,
                compacted_chars=0,
                from_turn=0,
                to_turn=0,
                reason_code=HAND_DISTILLED_SKIP,
                reason_detail=detail,
                source_path=source_path,
                payload_extra={
                    "distill_mode": DISTILL_MODE_SKIPPED,
                    "hand_distilled_paths": list(paths),
                },
            )
            visible = self._insert_items(
                [placeholder],
                actor=actor,
                reason_code=HAND_DISTILLED_SKIP,
                summary="hand_distilled skip",
            )
            touched.extend(visible)
        visible = self._merge_declared_paths(
            visible, paths, mode=DISTILL_MODE_SKIPPED, actor=actor
        )
        if touched:
            self._audit_hand_distilled(
                touched,
                mode=DISTILL_MODE_SKIPPED,
                paths=paths,
                actor=actor,
                declaration="skip",
                summary="hand_distilled: session taken out of distillation",
            )
        return {
            "action": "excluded",
            "reason_code": HAND_DISTILLED_SKIP,
            "session_key": key,
            "hand_distilled_paths": list(paths),
            "items": visible,
        }

    def on_hand_distilled_skip(
        self,
        session_key: str,
        *,
        wiki_paths: list[str] | None = None,
        actor: str = "mcp",
    ) -> dict[str, Any]:
        """``hand_distilled`` with ``governance_only = false``.

        The client wrote the knowledge up by hand and wants no distillation at all:
        the unconsumed items are excluded and the session is taken out for good. A
        later archive event never re-opens it; only an explicit ``enqueue`` does.
        """
        key = (session_key or "").strip()
        if not key:
            raise ValueError("session_key is required to hand_distilled")
        session = self._ensure_session(key)
        declared = self._hand_distilled_declaration(key, session)
        declared_paths = declared["hand_distilled_paths"] if declared is not None else []
        # The documents of an earlier declaration are as hand-written as the new ones.
        paths = list(dict.fromkeys([*redact_hand_distilled_paths(wiki_paths), *declared_paths]))
        meta = dict(session.metadata) if isinstance(session.metadata, dict) else {}
        session.metadata = {
            **meta,
            **_declaration_meta(
                DISTILL_MODE_SKIPPED, paths, declared_at=str(meta.get("hand_distilled_at") or "")
            ),
        }
        session = self.db.upsert_session(session, preserve_existing_lifecycle=True)
        return self._apply_hand_distilled_skip(
            session,
            paths=paths,
            channel=IngressChannel.MCP_EXPLICIT,
            actor=actor,
            detail="hand-distilled by the client: no further distillation",
        )

    # ---------------- ingress 2: archive ----------------

    def on_archive(
        self,
        client: str,
        session_id: str,
        *,
        title: str = "",
        project: str = "generic",
        workspace: str = "",
        transcript_path: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        channel: IngressChannel = IngressChannel.CODEX_ARCHIVE,
        actor: str = "hook",
        source_path: str | None = None,
        parent_session_key: str = "",
        is_subagent: bool = False,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """An archived conversation: opt-out veto, delta check, triage, then queue.

        ``now_ts`` is the clock the Antigravity quiet window is measured against (epoch
        seconds; the wall clock when omitted).

        A transcript that is still being written is not an archived conversation. Antigravity
        has no archive directory — its transcript is simply a file that keeps growing — so one
        inside the quiet window (``sessions.scan_quiet_window_seconds``) answers ``skipped``
        with ``QUIET_WINDOW_ACTIVE`` before anything about the session or its queue history
        is touched; a later pass archives it once it has gone quiet. An explicit enqueue is a
        person's decision and is never held back (see ``on_explicit_enqueue``).
        """
        client_name = (client or "generic").strip() or "generic"
        sid = (session_id or "").strip()
        t_path = Path(transcript_path) if transcript_path else (Path(source_path) if source_path else None)
        meta: dict[str, Any] = {}
        if t_path and t_path.exists() and t_path.suffix.lower() == ".jsonl":
            meta = extract_jsonl_metadata(t_path, client=client_name)
            canonical_id = meta.get("canonical_session_id")
            if canonical_id:
                sid = canonical_id
            if not workspace and meta.get("workspace"):
                workspace = meta["workspace"]
            if not parent_session_key and meta.get("parent_session_id"):
                parent_session_key = f"{client_name}:{meta['parent_session_id']}"
            if meta.get("is_subagent"):
                is_subagent = True

        if client_name == "codex" and sid:
            uuid_matches = _UUID_RE.findall(sid)
            if uuid_matches:
                sid = uuid_matches[-1]

        if not sid:
            return {"action": "skipped", "reason_code": "MISSING_SESSION_ID", "items": []}

        # Gate 0: a transcript still being written is only half a conversation.
        if self._transcript_is_being_written(t_path, client_name, now_ts=now_ts):
            logger.info(
                "queue: transcript still being written, left for a later pass: %s:%s",
                client_name,
                sid,
            )
            return {"action": "skipped", "reason_code": QUIET_WINDOW_ACTIVE, "items": []}

        skey = f"{client_name}:{sid}"
        existing = self.db.get_session(skey)
        resolved_title = derive_readable_title(
            client_name,
            sid,
            explicit_title=title or (existing.title if existing else ""),
            custom_title=meta.get("custom_title", ""),
            text=meta.get("first_user_message", "") or (content or ""),
            workspace=workspace or (existing.workspace if existing else ""),
        )
        existing_meta = dict(existing.metadata) if existing is not None and isinstance(existing.metadata, dict) else {}
        merged_meta = {**existing_meta}
        if "discovered_by" not in merged_meta:
            merged_meta["discovered_by"] = "on_archive"
        if parent_session_key:
            merged_meta["parent_session_key"] = parent_session_key
        if is_subagent:
            merged_meta["is_subagent"] = True
        if meta.get("subagent_nickname"):
            merged_meta["subagent_nickname"] = meta["subagent_nickname"]
        if meta.get("subagent_role"):
            merged_meta["subagent_role"] = meta["subagent_role"]
        # A hand-distilled declaration outlives the metadata it was first mirrored into:
        # a later registration replaces that metadata wholesale, the queue history does not.
        declared = self._hand_distilled_declaration(skey, existing)
        distill_mode = declared["distill_mode"] if declared is not None else DISTILL_MODE_FULL
        distill_paths = declared["hand_distilled_paths"] if declared is not None else []
        if declared is not None:
            merged_meta.update(
                _declaration_meta(
                    distill_mode,
                    distill_paths,
                    declared_at=str(existing_meta.get("hand_distilled_at") or ""),
                )
            )

        record = SessionRecord(
            session_key=skey,
            client=client_name,
            session_id=sid,
            title=resolved_title,
            project=(
                project
                if project and project.strip().lower() not in {"", "generic"}
                else (existing.project if existing else "generic")
            ),
            workspace=workspace or (existing.workspace if existing else ""),
            transcript_path=transcript_path or (existing.transcript_path if existing else None),
            summary=summary if summary is not None else (existing.summary if existing else None),
            content=content if content is not None else (existing.content if existing else None),
            tags=list(dict.fromkeys([*(tags or []), *((existing.tags if existing else []) or [])])),
            priority=(existing.priority if existing else "") or "P1_GENERAL",
            status=(existing.status if existing else SessionStatus.PENDING),
            metadata=merged_meta,
        )
        session = self.db.upsert_session(record, preserve_existing_lifecycle=True)

        text = self._session_text(session)
        content_hash = compute_content_hash(text)
        total_turns = len(split_dialogue_turns(text))

        # Gate 1: an explicit opt-out (profile flag, tag, or in-text marker) vetoes.
        if session.opt_out or has_explicit_opt_out(text, tags=session.tags, metadata=session.metadata):
            return self._exclude_for_opt_out(
                session,
                content_hash=content_hash,
                total_chars=len(text),
                total_turns=total_turns,
                channel=channel,
                actor=actor,
                source_path=source_path,
            )

        # Gate 2: no new content and no new turns -> do not manufacture inventory.
        if self._is_unchanged(session, content_hash=content_hash, total_turns=total_turns):
            return {"action": "unchanged", "reason_code": "", "items": []}

        # Gate 2b: the client hand-distilled this session and took it out. A passive
        # archive event never re-opens it, however much the conversation grew.
        if distill_mode == DISTILL_MODE_SKIPPED:
            return self._apply_hand_distilled_skip(
                session,
                paths=distill_paths,
                channel=channel,
                actor=actor,
                source_path=source_path,
                detail="hand-distilled by the client: no further distillation",
            )

        # Gate 3: deterministic triage rules (0-token pre-filter). A governance-only
        # session is an explicit declaration, so the noise rules do not apply to it.
        if distill_mode == DISTILL_MODE_GOVERNANCE_ONLY:
            triage: dict[str, Any] = {
                "admit": True,
                "tier": "explicit",
                "reason": "hand_distilled_governance_only",
                "reason_code": "",
                "workspace": session.workspace,
            }
        else:
            open_retro = any(
                retro.session_key == skey
                for retro in self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=200)
            )
            triage = triage_session(
                session,
                text,
                self.cfg.sessions.triage,
                has_open_retro=open_retro,
            )
        superseded = self._supersede_queued_items(skey, actor=actor)
        revision = self.db.get_latest_queue_revision(skey) + 1
        if not triage["admit"]:
            reason_code = triage.get("reason_code") or map_triage_reason_code(
                triage.get("reason", "")
            )
            item = self._build_item(
                session,
                revision=revision,
                chunk={"chunk_text": "", "turn_start": 0, "turn_end": 0},
                chunk_index=1,
                chunk_total=1,
                channel=channel,
                status=QueueStatus.EXCLUDED,
                priority=session.priority,
                content_hash=content_hash,
                total_chars=len(text),
                compacted_chars=0,
                from_turn=0,
                to_turn=total_turns,
                reason_code=reason_code,
                reason_detail=str(triage.get("reason") or ""),
                source_path=source_path,
            )
            saved = self._insert_items(
                [item],
                actor=actor,
                reason_code=reason_code,
                summary=f"triage excluded: {triage.get('reason', '')}",
            )
            self.db.update_session_watermark(
                skey, latest_content_hash=content_hash, latest_turn_count=total_turns
            )
            return {
                "action": "excluded",
                "reason_code": reason_code,
                "items": saved,
                "superseded": superseded,
            }

        # Admitted: only the delta after the consumption watermark is queued.
        from_turn = (
            session.last_consumed_turn
            if 0 < session.last_consumed_turn < total_turns
            else 0
        )
        prepared = prepare_queue_chunks(
            text, from_turn=from_turn, max_item_chars=self.max_item_chars
        )
        items = self._build_chunk_items(
            session,
            prepared,
            revision=revision,
            channel=channel,
            status=QueueStatus.QUEUED,
            priority=session.priority or "P1_GENERAL",
            content_hash=content_hash,
            source_path=source_path,
            # A later revision of a hand-distilled session stays governance-only until
            # an explicit enqueue resets it, so a continued conversation never writes
            # a second copy of what the client wrote by hand.
            payload_extra=(
                self._declaration_payload(distill_mode, distill_paths, {}, text)
                if distill_mode != DISTILL_MODE_FULL
                else None
            ),
        )
        saved = self._insert_items(
            items, actor=actor, summary=f"archive enqueue via {channel.value}"
        )
        self.db.update_session_watermark(
            skey, latest_content_hash=content_hash, latest_turn_count=total_turns
        )
        return {
            "action": "enqueued",
            "reason_code": "",
            "items": saved,
            "superseded": superseded,
        }

    @staticmethod
    def _item_represents(item: DistillQueueItem, content_hash: str) -> bool:
        """Whether a queue item was built from exactly this transcript.

        An item that carries a structured brief is fingerprinted over transcript and
        brief together. It also remembers the transcript's own fingerprint, which is
        what an archive event arrives with: the brief must not make the same
        conversation look like new content.
        """
        if item.content_hash == content_hash:
            return True
        payload = item.payload if isinstance(item.payload, dict) else {}
        return bool(payload.get("text_hash")) and payload.get("text_hash") == content_hash

    def _is_unchanged(
        self, session: SessionRecord, *, content_hash: str, total_turns: int
    ) -> bool:
        """Whether this content is already represented in the queue history."""
        if content_hash == session.last_consumed_hash and total_turns <= session.last_consumed_turn:
            return True
        items = self.db.list_session_queue_items(session.session_key)
        if not items:
            return False
        for item in items:
            if item.status in {QueueStatus.QUEUED, QueueStatus.PROCESSING} and self._item_represents(
                item, content_hash
            ):
                return True
        newest_revision = items[0].revision
        newest = [item for item in items if item.revision == newest_revision]
        if not newest or any(not self._item_represents(item, content_hash) for item in newest):
            return False
        return total_turns <= max(item.consumed_to_turn for item in newest)

    def _exclude_for_opt_out(
        self,
        session: SessionRecord,
        *,
        content_hash: str,
        total_chars: int,
        total_turns: int,
        channel: IngressChannel,
        actor: str,
        source_path: str | None,
        reason_detail: str = "user opted out of distillation",
    ) -> dict[str, Any]:
        """Gate 1: the veto keeps queued work visible as ``excluded(USER_OPT_OUT)``."""
        skey = session.session_key
        if not session.opt_out:
            self.db.set_session_opt_out(skey, True, reason_detail)
        items: list[DistillQueueItem] = []
        for item in self.db.list_session_queue_items(skey, statuses=[QueueStatus.QUEUED]):
            updated = self._transition(
                item,
                QueueStatus.EXCLUDED,
                reason_code="USER_OPT_OUT",
                detail=reason_detail,
                actor=actor,
                summary="opt-out veto",
            )
            if updated is not None:
                items.append(updated)
        visible = [
            item
            for item in self.db.list_session_queue_items(skey)
            if item.exclude_reason_code == "USER_OPT_OUT"
        ]
        if not visible and not items:
            revision = self.db.get_latest_queue_revision(skey) + 1
            item = self._build_item(
                session,
                revision=revision,
                chunk={"chunk_text": "", "turn_start": 0, "turn_end": 0},
                chunk_index=1,
                chunk_total=1,
                channel=channel,
                status=QueueStatus.EXCLUDED,
                priority=session.priority,
                content_hash=content_hash,
                total_chars=total_chars,
                compacted_chars=0,
                from_turn=0,
                to_turn=total_turns,
                reason_code="USER_OPT_OUT",
                reason_detail=reason_detail,
                source_path=source_path,
            )
            items.extend(
                self._insert_items(
                    [item], actor=actor, reason_code="USER_OPT_OUT", summary="opt-out veto"
                )
            )
        return {"action": "excluded", "reason_code": "USER_OPT_OUT", "items": items}

    # ---------------- ingress 3: withdraw ----------------

    def on_withdraw(
        self,
        session_key: str,
        *,
        reason_code: str = "USER_WITHDRAW",
        detail: str = "",
        actor: str = "system",
    ) -> list[DistillQueueItem]:
        """Withdraw every still-queued item of a session (``processing`` is left alone)."""
        key = (session_key or "").strip()
        if not key:
            return []
        withdrawn: list[DistillQueueItem] = []
        for item in self.db.list_session_queue_items(key, statuses=[QueueStatus.QUEUED]):
            updated = self._transition(
                item,
                QueueStatus.WITHDRAWN,
                reason_code=reason_code,
                actor=actor,
                summary=detail or "queue item withdrawn",
            )
            if updated is not None:
                withdrawn.append(updated)
        return withdrawn

    # ---------------- ingress 4: opt-out ----------------

    def on_opt_out(
        self,
        session_key: str,
        *,
        reason: str = "",
        actor: str = "mcp",
    ) -> dict[str, Any]:
        """Freeze a session out of the queue for good (until explicitly cleared)."""
        key = (session_key or "").strip()
        if not key:
            raise ValueError("session_key is required to opt out")
        session = self._ensure_session(key)
        detail = reason or "user opted out of distillation"
        self.db.set_session_opt_out(key, True, detail)
        items: list[DistillQueueItem] = []
        for item in self.db.list_session_queue_items(
            key, statuses=[QueueStatus.QUEUED, QueueStatus.WITHDRAWN]
        ):
            updated = self._transition(
                item,
                QueueStatus.EXCLUDED,
                reason_code="USER_OPT_OUT",
                detail=detail,
                actor=actor,
                summary="user opt-out",
            )
            if updated is not None:
                items.append(updated)
        visible = [
            item
            for item in self.db.list_session_queue_items(key)
            if item.exclude_reason_code == "USER_OPT_OUT"
        ]
        if not visible:
            revision = self.db.get_latest_queue_revision(key) + 1
            item = self._build_item(
                session,
                revision=revision,
                chunk={"chunk_text": "", "turn_start": 0, "turn_end": 0},
                chunk_index=1,
                chunk_total=1,
                channel=IngressChannel.MCP_EXPLICIT,
                status=QueueStatus.EXCLUDED,
                priority=session.priority,
                content_hash="",
                total_chars=0,
                compacted_chars=0,
                from_turn=0,
                to_turn=0,
                reason_code="USER_OPT_OUT",
                reason_detail=detail,
            )
            items.extend(
                self._insert_items(
                    [item], actor=actor, reason_code="USER_OPT_OUT", summary="user opt-out"
                )
            )
        return {
            "action": "excluded",
            "session_key": key,
            "reason_code": "USER_OPT_OUT",
            "items": items,
        }

    # ---------------- actions / re-triage / reconcile / bridge ----------------

    def apply_item_action(
        self,
        queue_id: str,
        action: str,
        *,
        reason: str = "",
        actor: str = "portal",
    ) -> DistillQueueItem | None:
        """Apply one maintainer action to one queue item."""
        item = self.db.get_queue_item(queue_id)
        if item is None:
            return None
        normalized = str(action or "").strip().lower().replace("-", "_")
        if normalized in {"requeue", "retry"}:
            session = self.db.get_session(item.session_key)
            if session is not None and session.opt_out:
                raise ValueError("session is opted out; clear the opt-out before requeueing")
            return self._transition(
                item,
                QueueStatus.QUEUED,
                reason_code=reason or "MANUAL_REQUEUE",
                actor=actor,
                summary="manual requeue",
                reset_retry=True,
            )
        if normalized == "exclude":
            code = str(reason or "").strip()
            detail = ""
            if not code or not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
                detail = code
                code = "MANUAL_EXCLUDE"
            return self._transition(
                item,
                QueueStatus.EXCLUDED,
                reason_code=code,
                detail=detail or reason,
                actor=actor,
                summary="manual exclude",
            )
        if normalized == "withdraw":
            return self._transition(
                item,
                QueueStatus.WITHDRAWN,
                reason_code=reason or "USER_WITHDRAW",
                actor=actor,
                summary="manual withdraw",
            )
        if normalized == "opt_out":
            self.on_opt_out(item.session_key, reason=reason, actor=actor)
            return self.db.get_queue_item(queue_id)
        if normalized == "clear_opt_out":
            self.db.set_session_opt_out(item.session_key, False, "")
            self.db.append_audit_event(
                entity_type="session",
                entity_id=item.session_key,
                event_type="opt_out_cleared",
                actor=actor,
                summary="opt-out cleared",
                reason_code="USER_OPT_OUT_CLEARED",
            )
            return self.db.get_queue_item(queue_id)
        raise ValueError(f"unsupported queue action: {action}")


    # ---------------- read & intent helpers ----------------

    def load_queue_item_text(self, item: DistillQueueItem) -> str:
        """The text of one queue item: its pre-compacted chunk, else the session text.

        The first chunk of an item carries the client's structured brief after the
        dialogue, so the review reads the evidence with the conversation it belongs to.
        """
        payload = item.payload if isinstance(item.payload, dict) else {}
        chunk_text = payload.get("chunk_text")
        if isinstance(chunk_text, str) and chunk_text.strip():
            text = redact_text(chunk_text)
        else:
            session = self.db.get_session(item.session_key)
            if session is None:
                return ""
            text = self._session_text(session)
        brief = render_structured_brief(payload.get("structured_brief")) if item.chunk_index <= 1 else ""
        return f"{text}\n\n{brief}" if brief else text

    def handle_session_intent(
        self,
        action: str,
        *,
        session_key: str = "",
        client: str = "generic",
        session_id: str = "",
        title: str = "",
        project: str = "generic",
        workspace: str = "",
        transcript_path: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        focus_note: str = "",
        reason: str = "",
        actor: str = "mcp",
        wiki_paths: list[str] | None = None,
        governance_only: bool = True,
    ) -> dict[str, Any]:
        """The MCP declaration surface: ``enqueue`` / ``opt_out`` / ``withdraw`` / ``hand_distilled``."""
        normalized = str(action or "").strip().lower().replace("-", "_")
        key = (session_key or "").strip()
        client_name = (client or "generic").strip() or "generic"
        sid = (session_id or "").strip()
        if key and not sid and ":" in key:
            head, _, tail = key.partition(":")
            client_name, sid = head or client_name, tail
        elif key and not sid:
            sid = key
        resolved_key = key or (f"{client_name}:{sid}" if sid else "")

        if normalized == "hand_distilled":
            if not resolved_key:
                raise ValueError("session_key is required to hand_distilled")
            if governance_only:
                items = self.on_explicit_enqueue(
                    client=client_name,
                    session_id=sid,
                    title=title,
                    project=project,
                    workspace=workspace,
                    transcript_path=transcript_path,
                    summary=summary,
                    content=content,
                    focus_note=focus_note,
                    channel=IngressChannel.MCP_EXPLICIT,
                    actor=actor,
                    force=True,
                    priority="P0_CORE",
                    distill_mode=DISTILL_MODE_GOVERNANCE_ONLY,
                    hand_distilled_paths=wiki_paths,
                )
                reported = self._item_paths(items[0]) if items else redact_hand_distilled_paths(wiki_paths)
                return {
                    "status": "queued" if items else "unchanged",
                    "action": "hand_distilled",
                    "session_key": items[0].session_key if items else f"{client_name}:{sid}",
                    "distill_mode": DISTILL_MODE_GOVERNANCE_ONLY,
                    "hand_distilled_paths": reported,
                    "queue_ids": [item.queue_id for item in items],
                    "items": [self._item_brief(item) for item in items],
                }
            result = self.on_hand_distilled_skip(resolved_key, wiki_paths=wiki_paths, actor=actor)
            return {
                "status": "excluded",
                "action": "hand_distilled",
                "session_key": resolved_key,
                "distill_mode": DISTILL_MODE_SKIPPED,
                "reason_code": HAND_DISTILLED_SKIP,
                "hand_distilled_paths": result["hand_distilled_paths"],
                "queue_ids": [item.queue_id for item in result["items"]],
                "items": [self._item_brief(item) for item in result["items"]],
            }

        if normalized in {"enqueue", "queue", "ingest"}:
            if not sid:
                raise ValueError("session_id or session_key is required to enqueue")
            items = self.on_explicit_enqueue(
                client=client_name,
                session_id=sid,
                title=title,
                project=project,
                workspace=workspace,
                transcript_path=transcript_path,
                summary=summary,
                content=content,
                focus_note=focus_note,
                channel=IngressChannel.MCP_EXPLICIT,
                actor=actor,
                force=True,
            )
            return {
                "status": "queued" if items else "unchanged",
                "action": "enqueue",
                "session_key": f"{client_name}:{sid}",
                "queue_ids": [item.queue_id for item in items],
                "items": [self._item_brief(item) for item in items],
            }
        if normalized in {"opt_out", "exclude"}:
            if not resolved_key:
                raise ValueError("session_key is required to opt out")
            result = self.on_opt_out(resolved_key, reason=reason, actor=actor)
            return {
                "status": "excluded",
                "action": "opt_out",
                "session_key": resolved_key,
                "reason_code": "USER_OPT_OUT",
                "queue_ids": [item.queue_id for item in result["items"]],
                "items": [self._item_brief(item) for item in result["items"]],
            }
        if normalized in {"withdraw", "cancel"}:
            if not resolved_key:
                raise ValueError("session_key is required to withdraw")
            items = self.on_withdraw(
                resolved_key,
                reason_code="USER_WITHDRAW",
                detail=reason,
                actor=actor,
            )
            return {
                "status": "withdrawn",
                "action": "withdraw",
                "session_key": resolved_key,
                "queue_ids": [item.queue_id for item in items],
                "items": [self._item_brief(item) for item in items],
            }
        raise ValueError(f"unsupported session intent action: {action}")




