"""Hand-distilled session declaration ledger for the distill queue.

Extracted from :mod:`ai_flywheel.core.queue_service` to keep the ``hand_distilled``
dual-track policy (``governance_only`` vs ``skipped``), path merging, and audit
events cohesive and independently readable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ai_flywheel.core.queue_compaction import (
    DISTILL_MODES,
    DISTILL_MODE_FULL,
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_EVENT,
    HAND_DISTILLED_SKIP,
    compute_content_hash,
    redact_hand_distilled_paths,
)
from ai_flywheel.core.time_utils import shanghai_now_iso
from ai_flywheel.models import DistillQueueItem, IngressChannel, QueueStatus, SessionRecord

if TYPE_CHECKING:
    from ai_flywheel.storage.db import FlywheelDB


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


class QueueHandDistilledMixin:
    """Mixin on :class:`~ai_flywheel.core.queue_service.DistillQueueService`
    owning ``hand_distilled`` declarations, path merging, and skip transitions.
    """

    db: FlywheelDB

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
            updated = self._transition(  # type: ignore[attr-defined]
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
            placeholder = self._build_item(  # type: ignore[attr-defined]
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
            visible = self._insert_items(  # type: ignore[attr-defined]
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
        session = self._ensure_session(key)  # type: ignore[attr-defined]
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
