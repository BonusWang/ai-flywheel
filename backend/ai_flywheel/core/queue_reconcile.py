"""Queue reconciliation, re-triage sweep, and legacy pending-session bridging.

Separated from :mod:`ai_flywheel.core.queue_service` so batch maintenance flows
(Codex strict-archive sync, re-triage sweeps, and grown-transcript bridging)
evolve independently of real-time ingress transitions.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ai_flywheel.core.queue_compaction import (
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_SKIP,
    _path_is_within,
    _safe_mtime,
    _scan_source_fields,
)
from ai_flywheel.core.triage import (
    _UUID_RE,
    _is_explicit_submission,
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

logger = logging.getLogger("ai_flywheel.queue")

#: One reconciliation pass looks at the newest files of a source; the scan is a
#: lightweight poll, not a full-history import.
MAX_ARCHIVE_FILES_PER_SOURCE = 500

#: Excluded items with these codes are user/system decisions and are never
#: re-opened by a rules re-triage.
RE_TRIAGE_SKIP_REASON_CODES = frozenset(
    {"USER_OPT_OUT", "MANUAL_EXCLUDE", "SUPERSEDED", "HAND_DISTILLED_SKIP"}
)

#: How much of a session's opening a re-triage reads to recognise a write dispatch. A
#: verdict taken on an excerpt this long is confirmed on the whole conversation.
MAX_OPENING_CHARS = 24000
_OPENING_USER_TURN_RE = re.compile(r"^\[(?:user|human)\]", re.IGNORECASE | re.MULTILINE)

#: Why a passive pass archived nothing: the transcript was still being written.
QUIET_WINDOW_ACTIVE = "QUIET_WINDOW_ACTIVE"


class QueueReconcileMixin:
    """Batch reconciliation, re-triage, and pending-session bridging mixed into DistillQueueService."""

    def re_triage_queue(
        self, *, dry_run: bool = True, actor: str = "portal"
    ) -> dict[str, Any]:
        """Re-evaluate the current triage rules over queued and rule-excluded items.

        ``dry_run=True`` computes the same change list and metadata refresh count with zero DB mutations.
        ``dry_run=False`` updates item metadata (title, workspace, parentage), marks superseded items
        as EXCLUDED (SUPERSEDED), and applies state transitions.
        """
        candidates = [
            *self.db.list_queue_items(status=QueueStatus.QUEUED, limit=1000),
            *self.db.list_queue_items(status=QueueStatus.EXCLUDED, limit=1000),
        ]
        open_retro_keys = {
            retro.session_key
            for retro in self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=200)
        }
        changes: list[dict[str, Any]] = []
        metadata_refresh_count = 0
        # How each session opens, read once: a write dispatch is recognised by its
        # opening instruction, which only the source transcript still shows in full.
        openings: dict[str, str] = {}
        full_texts: dict[str, str] = {}

        for item in candidates:
            if (
                item.status is QueueStatus.EXCLUDED
                and item.exclude_reason_code in RE_TRIAGE_SKIP_REASON_CODES
            ):
                continue
            session = self.db.get_session(item.session_key)
            if session is None or session.opt_out:
                continue

            # A hand-distilled declaration is a user decision, not a triage verdict: the
            # noise rules never drop a governance-only item, and a session the client took
            # out by hand is never queued again.
            item_mode = self._item_distill_mode(item)
            if item.status is QueueStatus.QUEUED:
                if item_mode == DISTILL_MODE_GOVERNANCE_ONLY:
                    continue
                session_meta = session.metadata if isinstance(session.metadata, dict) else {}
                if item_mode == DISTILL_MODE_SKIPPED or session_meta.get("hand_distilled_skip") is True:
                    detail = "hand-distilled by the client: no further distillation"
                    changes.append(
                        {
                            "queue_id": item.queue_id,
                            "session_key": item.session_key,
                            "from_status": QueueStatus.QUEUED.value,
                            "to_status": QueueStatus.EXCLUDED.value,
                            "reason_code": HAND_DISTILLED_SKIP,
                            "detail": detail,
                        }
                    )
                    if not dry_run:
                        self._transition(
                            item,
                            QueueStatus.EXCLUDED,
                            reason_code=HAND_DISTILLED_SKIP,
                            detail=detail,
                            actor=actor,
                            summary="re-triage excluded the item",
                        )
                    continue

            # Read source transcript if available to refresh metadata and identity
            source_file: Path | None = None
            if item.source_path and Path(item.source_path).exists():
                source_file = Path(item.source_path)
            elif session.transcript_path and Path(session.transcript_path).exists():
                source_file = Path(session.transcript_path)

            meta: dict[str, Any] = {}
            if source_file and source_file.suffix.lower() == ".jsonl":
                meta = extract_jsonl_metadata(source_file, client=item.client)

            canonical_id = meta.get("canonical_session_id")
            if not canonical_id and item.client == "codex":
                matches = _UUID_RE.findall(item.session_key)
                if matches:
                    canonical_id = matches[0]
            canonical_key = f"{item.client}:{canonical_id}" if canonical_id else item.session_key

            parent_sid = meta.get("parent_session_id", "")
            parent_key = f"{item.client}:{parent_sid}" if parent_sid else (item.parent_session_key or "")
            is_sub = bool(meta.get("is_subagent") or item.is_subagent or parent_key)
            ws = meta.get("workspace") or item.workspace
            item_text = self.load_queue_item_text(item)
            # A write dispatch is recognised by how the session opens and whether anything
            # follows the instruction — never by one compacted chunk of it.
            opening_text = self._session_opening_text(session, openings)
            new_title = derive_readable_title(
                item.client,
                canonical_id or item.session_key,
                explicit_title=item.title,
                custom_title=meta.get("custom_title", ""),
                text=meta.get("first_user_message") or item_text,
                workspace=ws,
            )

            title_changed = bool(new_title and new_title != item.title)
            ws_changed = bool(ws and ws != item.workspace)
            parent_changed = bool(parent_key != item.parent_session_key)
            sub_changed = bool(is_sub != item.is_subagent)

            if title_changed or ws_changed or parent_changed or sub_changed:
                metadata_refresh_count += 1
                if not dry_run:
                    payload = dict(item.payload) if isinstance(item.payload, dict) else {}
                    if meta.get("subagent_nickname"):
                        payload["subagent_nickname"] = meta["subagent_nickname"]
                    if meta.get("subagent_role"):
                        payload["subagent_role"] = meta["subagent_role"]
                    if parent_key:
                        payload["parent_session_key"] = parent_key
                    if is_sub:
                        payload["is_subagent"] = True
                    self.db.update_queue_item_metadata(
                        item.queue_id,
                        title=new_title or None,
                        workspace=ws or None,
                        parent_session_key=parent_key,
                        is_subagent=is_sub,
                        payload=payload,
                    )
                    sess_meta = dict(session.metadata) if isinstance(session.metadata, dict) else {}
                    sess_changed = False
                    if title_changed and new_title:
                        session.title = new_title
                        sess_changed = True
                    if ws_changed and ws:
                        session.workspace = ws
                        sess_changed = True
                    if parent_key and sess_meta.get("parent_session_key") != parent_key:
                        sess_meta["parent_session_key"] = parent_key
                        sess_changed = True
                    if is_sub and not sess_meta.get("is_subagent"):
                        sess_meta["is_subagent"] = True
                        sess_changed = True
                    if meta.get("subagent_nickname") and sess_meta.get("subagent_nickname") != meta["subagent_nickname"]:
                        sess_meta["subagent_nickname"] = meta["subagent_nickname"]
                        sess_changed = True
                    if meta.get("subagent_role") and sess_meta.get("subagent_role") != meta["subagent_role"]:
                        sess_meta["subagent_role"] = meta["subagent_role"]
                        sess_changed = True
                    if sess_changed:
                        session.metadata = sess_meta
                        self.db.upsert_session(session)

            # Check if this queued item is superseded by a canonical queue item
            if item.status is QueueStatus.QUEUED and item.session_key != canonical_key:
                active_canonical = [
                    ci
                    for ci in self.db.list_session_queue_items(canonical_key)
                    if ci.status in {QueueStatus.QUEUED, QueueStatus.PROCESSING, QueueStatus.PROCESSED}
                ]
                if active_canonical:
                    changes.append(
                        {
                            "queue_id": item.queue_id,
                            "session_key": item.session_key,
                            "from_status": QueueStatus.QUEUED.value,
                            "to_status": QueueStatus.EXCLUDED.value,
                            "reason_code": "SUPERSEDED",
                            "detail": f"superseded by canonical {canonical_key}",
                        }
                    )
                    if not dry_run:
                        self._transition(
                            item,
                            QueueStatus.EXCLUDED,
                            reason_code="SUPERSEDED",
                            detail=f"superseded by canonical {canonical_key}",
                            actor=actor,
                            summary=f"re-triage excluded: superseded by {canonical_key}",
                        )
                    continue

            session_for_triage = session
            if parent_key and not (isinstance(session.metadata, dict) and session.metadata.get("parent_session_key")):
                meta_copy = dict(session.metadata) if isinstance(session.metadata, dict) else {}
                meta_copy["parent_session_key"] = parent_key
                session_for_triage = session.model_copy(update={"metadata": meta_copy})
            result = triage_session(
                session_for_triage,
                item_text,
                self.cfg.sessions.triage,
                has_open_retro=item.session_key in open_retro_keys,
                opening_text=opening_text,
            )
            if (
                result.get("reason") == "controlled_write_dispatch"
                and len(opening_text) >= MAX_OPENING_CHARS
            ):
                # The opening may end before a later question: whether anything follows
                # the instruction is answered on the whole conversation.
                result = triage_session(
                    session_for_triage,
                    item_text,
                    self.cfg.sessions.triage,
                    has_open_retro=item.session_key in open_retro_keys,
                    opening_text=self._session_full_text(session, full_texts),
                )
            if result["admit"] and item.status is QueueStatus.EXCLUDED:
                changes.append(
                    {
                        "queue_id": item.queue_id,
                        "session_key": item.session_key,
                        "from_status": QueueStatus.EXCLUDED.value,
                        "to_status": QueueStatus.QUEUED.value,
                        "reason_code": "",
                        "detail": str(result.get("reason") or ""),
                    }
                )
                if not dry_run:
                    self._transition(
                        item,
                        QueueStatus.QUEUED,
                        reason_code="RE_TRIAGE_ADMIT",
                        actor=actor,
                        summary="re-triage admitted the item",
                        reset_retry=True,
                    )
            elif not result["admit"] and item.status is QueueStatus.QUEUED:
                reason_code = result.get("reason_code") or map_triage_reason_code(
                    result.get("reason", "")
                )
                changes.append(
                    {
                        "queue_id": item.queue_id,
                        "session_key": item.session_key,
                        "from_status": QueueStatus.QUEUED.value,
                        "to_status": QueueStatus.EXCLUDED.value,
                        "reason_code": reason_code,
                        "detail": str(result.get("reason") or ""),
                    }
                )
                if not dry_run:
                    self._transition(
                        item,
                        QueueStatus.EXCLUDED,
                        reason_code=reason_code,
                        detail=str(result.get("reason") or ""),
                        actor=actor,
                        summary="re-triage excluded the item",
                    )
        would_exclude = [c for c in changes if c["to_status"] == QueueStatus.EXCLUDED.value]
        would_admit = [c for c in changes if c["to_status"] == QueueStatus.QUEUED.value]
        return {
            "dry_run": bool(dry_run),
            "evaluated": len(candidates),
            "checked": len(candidates),
            "change_count": len(changes),
            "changed_count": len(changes),
            "changes": changes,
            "metadata_refresh_count": metadata_refresh_count,
            "would_exclude": would_exclude,
            "would_admit": would_admit,
            "summary": self.db.summarize_queue(),
        }

    def reconcile_codex_archives(self) -> dict[str, Any]:
        """Sync the configured Codex archive sources with the queue inventory.

        A file that appears (or grows) is archived in; a still-queued item whose
        archived file was removed is withdrawn automatically. A transcript still being
        written (an Antigravity one inside its quiet window) is counted under
        ``skipped_active`` and left for a later pass.
        """
        report: dict[str, Any] = {
            "sources": [],
            "scanned": 0,
            "enqueued": 0,
            "excluded": 0,
            "unchanged": 0,
            "withdrawn": 0,
            "skipped_active": 0,
            "errors": [],
        }
        sources = self.cfg.sessions.codex_archive_sources
        if not sources:
            return report
        for src in sources:
            src_client, src_path, src_glob = _scan_source_fields(src)
            root = self.cfg.resolve_path(src_path)
            source_report: dict[str, Any] = {
                "client": src_client,
                "path": src_path,
                "scanned": 0,
                "enqueued": 0,
                "excluded": 0,
                "unchanged": 0,
                "withdrawn": 0,
                "skipped_active": 0,
            }
            if not root.exists():
                report["errors"].append(f"archive_source_missing:{src_client}:{src_path}")
                report["sources"].append(source_report)
                continue
            files = [path for path in root.glob(src_glob) if path.is_file()]
            files.sort(key=_safe_mtime, reverse=True)
            for path in files[:MAX_ARCHIVE_FILES_PER_SOURCE]:
                source_report["scanned"] += 1
                try:
                    meta = extract_jsonl_metadata(path, client=src_client)
                    canonical_id = meta.get("canonical_session_id") or path.stem
                    ws = meta.get("workspace") or ""
                    title = derive_readable_title(
                        src_client,
                        canonical_id,
                        custom_title=meta.get("custom_title", ""),
                        text=meta.get("first_user_message", ""),
                        workspace=ws,
                    )
                    parent_sid = meta.get("parent_session_id", "")
                    parent_skey = f"{src_client}:{parent_sid}" if parent_sid else ""
                    result = self.on_archive(
                        src_client,
                        canonical_id,
                        title=title,
                        project="generic",
                        workspace=ws,
                        transcript_path=str(path),
                        channel=IngressChannel.CODEX_ARCHIVE,
                        actor="archive_sync",
                        source_path=str(path),
                        parent_session_key=parent_skey,
                        is_subagent=bool(meta.get("is_subagent")),
                    )
                except Exception as exc:  # one bad file never stops the pass
                    report["errors"].append(
                        f"archive_ingest_failed:{src_client}:{path.name}:{exc}"
                    )
                    logger.exception("failed to ingest archived session %s", path)
                    continue
                action = result.get("action")
                if action == "enqueued":
                    source_report["enqueued"] += 1
                elif action == "excluded":
                    source_report["excluded"] += 1
                elif result.get("reason_code") == QUIET_WINDOW_ACTIVE:
                    # Not "unchanged": the file was never read this pass.
                    source_report["skipped_active"] += 1
                else:
                    source_report["unchanged"] += 1

            # A queued archive item whose file disappeared was un-archived.
            for item in self.db.list_queue_items(
                status=QueueStatus.QUEUED,
                ingress_channel=IngressChannel.CODEX_ARCHIVE,
                client=src_client,
                limit=1000,
            ):
                if not item.source_path:
                    continue
                if not _path_is_within(item.source_path, root):
                    continue
                if Path(item.source_path).exists():
                    continue
                removed = self.on_withdraw(
                    item.session_key,
                    reason_code="CODEX_UNARCHIVED",
                    detail=str(item.source_path),
                    actor="archive_sync",
                )
                source_report["withdrawn"] += len(removed)

            for key in ("scanned", "enqueued", "excluded", "unchanged", "withdrawn", "skipped_active"):
                report[key] += source_report[key]
            report["sources"].append(source_report)
        return report

    def bridge_pending_sessions_to_queue(
        self, *, limit: int = 500, actor: str = "p00_bridge"
    ) -> int:
        """Idempotently give legacy ``pending`` sessions a queue row (returns how many).

        Explicit user submissions (inline content / ``manual`` tag) bypass triage;
        disk-scanned or legacy hook sessions pass through the 3-tier archive gate
        so empty stubs and low-signal fragments never flood ``queued``.

        It also archives what grew: see ``_bridge_grown_transcripts``.
        """
        created = self._bridge_grown_transcripts(actor=actor)
        for session in self.db.list_pending_sessions_prioritized(limit=max(1, int(limit))):
            if self.db.get_latest_queue_revision(session.session_key) > 0:
                continue
            if _is_explicit_submission(session):
                items = self.on_explicit_enqueue(
                    session,
                    channel=IngressChannel.LEGACY_SCAN,
                    actor=actor,
                    force=False,
                    priority=session.priority or "P1_GENERAL",
                    focus_note="legacy pending session bridge",
                )
                created += len(items)
            else:
                res = self.on_archive(
                    client=session.client,
                    session_id=session.session_id,
                    title=session.title,
                    project=session.project,
                    workspace=session.workspace,
                    transcript_path=session.transcript_path,
                    summary=session.summary,
                    content=session.content,
                    tags=session.tags,
                    channel=IngressChannel.LEGACY_SCAN,
                    actor=actor,
                )
                if res.get("action") == "enqueued":
                    created += len(res.get("items") or [])
        return created

    def _bridge_grown_transcripts(self, *, actor: str) -> int:
        """Archive the turns each registered transcript gained since it was last archived.

        Antigravity has no archive directory and no archive event: a transcript is a file
        that keeps growing under the same session id. The scan registers it once; without
        this pass the conversation could continue for the rest of the day and nothing after
        the first archive would reach the queue. ``SessionAdapter.reconcile_scan_sources``
        hands over the transcripts it found quiet again and changed since. Each goes
        through the archive gate like any archived conversation — opt-out veto,
        hand-distilled declaration and the delta after the consumption watermark included —
        so only the turns that are new are queued, as the next revision.

        A transcript is marked as taken only once the queue has had it. One that was
        not — written to again before this pass reached it, or failed — keeps its marks and
        the next scan offers it again.
        """
        enqueued = 0
        for session_key, mtime, size in self.sessions.take_grown_transcripts():
            session = self.db.get_session(session_key)
            if session is None or session.opt_out:
                # Frozen since the scan looked: there is nothing in it the queue may take.
                continue
            try:
                res = self.on_archive(
                    client=session.client,
                    session_id=session.session_id,
                    title=session.title,
                    project=session.project,
                    workspace=session.workspace,
                    transcript_path=session.transcript_path,
                    summary=session.summary,
                    content=session.content,
                    tags=session.tags,
                    channel=IngressChannel.LEGACY_SCAN,
                    actor=actor,
                )
            except Exception:  # one bad transcript never stops the pass
                logger.exception("failed to archive the grown transcript of %s", session_key)
                continue
            if res.get("reason_code") == QUIET_WINDOW_ACTIVE:
                continue
            self.sessions.mark_transcript_scanned(session_key, mtime, size)
            if res.get("action") == "enqueued":
                enqueued += len(res.get("items") or [])
        return enqueued

    # ---------------- read helpers ----------------

    def _session_opening_text(self, session: SessionRecord, cache: dict[str, str]) -> str:
        """How a session opens, read once per pass; empty when its source is gone."""
        text = cache.get(session.session_key)
        if text is None:
            head = self.sessions.load_session_text(session, max_chars=MAX_OPENING_CHARS)
            text = head if _OPENING_USER_TURN_RE.search(head) else ""
            cache[session.session_key] = text
        return text

    def _session_full_text(self, session: SessionRecord, cache: dict[str, str]) -> str:
        """The whole conversation, read once per pass."""
        text = cache.get(session.session_key)
        if text is None:
            text = self._session_text(session)
            cache[session.session_key] = text
        return text

