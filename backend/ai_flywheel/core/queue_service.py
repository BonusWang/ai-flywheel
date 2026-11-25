"""The distill-queue state machine: four ingress events, three gates, one trail.

Every way a session can enter the queue converges here — Codex archive
reconciliation, the Claude Code ``SessionEnd`` hook, an MCP declaration, and a
manual portal submission — so the queue behaves identically whatever the entry
point. Each transition writes the structured ``audit_events`` trail
(``entity_type="queue_item"`` with ``from_status`` / ``to_status`` /
``reason_code``) and publishes ``queue_updated`` on the event broker.

Oversized conversations are handled deterministically and without a model call:
the transcript is read in full (never truncated at the 24k review limit), split
into dialogue turns, compacted turn by turn (the tail of a long tool output is
kept, so conclusions survive), and only then split into ordered chunks
``1/N .. N/N`` when the compacted text still exceeds ``queue_max_item_chars``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_flywheel.adapters.sessions import _extract_jsonl_text
from ai_flywheel.core.triage import (
    has_explicit_opt_out,
    map_triage_reason_code,
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

#: The archive/transcript reader is allowed to look at far more than one queue
#: item: truncating before compaction would silently drop the tail conclusions.
MAX_FULL_TRANSCRIPT_CHARS = 500_000

#: A long tool output / log block keeps its head and its tail; the middle folds.
MAX_COMPACT_TURN_CHARS = 2500
COMPACT_HEAD_CHARS = 1200
COMPACT_TAIL_CHARS = 800

#: One reconciliation pass looks at the newest files of a source; the scan is a
#: lightweight poll, not a full-history import.
MAX_ARCHIVE_FILES_PER_SOURCE = 500

#: Excluded items with these codes are user/system decisions and are never
#: re-opened by a rules re-triage.
RE_TRIAGE_SKIP_REASON_CODES = frozenset({"USER_OPT_OUT", "MANUAL_EXCLUDE", "SUPERSEDED"})


# ---------------------------------------------------------------- transcript reader


def read_full_transcript(path: Path, max_chars: int = MAX_FULL_TRANSCRIPT_CHARS) -> str:
    """Read a JSONL or plain-text transcript up to ``max_chars``, credentials masked.

    JSONL lines re-use ``_extract_jsonl_text`` from the session adapter, so the
    queue sees exactly the dialogue the review pipeline would see — only the
    length cap differs.
    """
    if not path.exists() or not path.is_file():
        return ""
    if path.suffix.lower() == ".jsonl":
        parts: list[str] = []
        total = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    text = _extract_jsonl_text(obj)
                    if not text:
                        continue
                    cleaned = redact_text(text)
                    parts.append(cleaned)
                    total += len(cleaned) + 1
                    if total >= max_chars:
                        break
        except OSError:
            return ""
        return "\n".join(parts)[:max_chars]

    try:
        return redact_text(path.read_text(encoding="utf-8", errors="ignore"))[:max_chars]
    except OSError:
        return ""


# ---------------------------------------------------------------- turn handling

_TURN_MARKER_LINE_RE = re.compile(r"(?im)^\[(?:user|assistant|human|model|tool)\]")
_ANY_ROLE_MARKER_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_-]*)\]")


def _role_of(turn: str) -> str:
    match = _ANY_ROLE_MARKER_RE.match(turn or "")
    return match.group(1).strip().lower() if match else ""


def split_dialogue_turns(text: str) -> list[str]:
    """Split a transcript into ordered dialogue turns.

    A line that starts with a role marker (``[user]`` / ``[assistant]`` ...) is
    a turn boundary. Text without any marker falls back to blank-line
    paragraphs, and only a text with neither becomes a single turn.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    positions = [match.start() for match in _TURN_MARKER_LINE_RE.finditer(raw)]
    if positions:
        turns: list[str] = []
        if positions[0] > 0:
            head = raw[: positions[0]].strip()
            if head:
                turns.append(head)
        for idx, start in enumerate(positions):
            end = positions[idx + 1] if idx + 1 < len(positions) else len(raw)
            segment = raw[start:end].strip()
            if segment:
                turns.append(segment)
        return turns
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw) if part.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    return [raw]


def compact_turn_text(turn: str, max_turn_chars: int = MAX_COMPACT_TURN_CHARS) -> str:
    """Deterministically shrink one long turn without losing its conclusion.

    A ``[user]`` turn is kept verbatim — the user's own words are the highest
    signal in the transcript — and so is untyped prose (it cannot be classified
    safely). A long assistant/tool turn keeps its head, a compact marker, and
    its tail, so the root cause and the final answer at the end are never lost.
    """
    text = (turn or "").strip()
    if len(text) <= max_turn_chars:
        return text
    role = _role_of(text)
    if role in {"user", "human", ""}:
        return text
    head = text[:COMPACT_HEAD_CHARS]
    tail = text[-COMPACT_TAIL_CHARS:]
    omitted = len(text) - COMPACT_HEAD_CHARS - COMPACT_TAIL_CHARS
    marker = f"[flywheel: {omitted} chars compacted from the middle of this {role} turn]"
    return f"{head}\n\n{marker}\n\n{tail}"


def _split_piece(text: str, limit: int) -> list[str]:
    """Split one turn that is still longer than the item cap (never drop a tail)."""
    if len(text) <= limit:
        return [text]
    return [text[start : start + limit] for start in range(0, len(text), limit)]


def prepare_queue_chunks(
    text: str,
    *,
    from_turn: int = 0,
    max_item_chars: int = 24000,
) -> dict[str, Any]:
    """Compact, then pack the dialogue into one or more ordered queue chunks.

    ``from_turn`` slices the delta when the session was already consumed up to a
    watermark (``0 < from_turn < len(turns)``). Each chunk carries its
    ``chunk_text`` together with the global ``turn_start`` / ``turn_end``
    boundaries, and adjacent chunks share the last turn as overlap context.
    """
    raw = text or ""
    turns = split_dialogue_turns(raw)
    total_turns = len(turns)
    limit = max(1000, int(max_item_chars))
    requested = int(from_turn or 0)
    start = requested if 0 < requested < total_turns else 0
    compacted = [compact_turn_text(turn) for turn in turns[start:]]
    compacted_chars = sum(len(turn) for turn in compacted)

    pieces: list[tuple[int, str]] = []
    for offset, turn in enumerate(compacted):
        for piece in _split_piece(turn, limit):
            pieces.append((start + offset, piece))

    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    chunk_start = start
    chunk_end = start
    overlap_text = ""
    overlap_index = 0

    def _flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        chunks.append(
            {
                "chunk_text": "\n\n".join(buffer),
                "turn_start": chunk_start,
                "turn_end": chunk_end,
            }
        )
        buffer = []

    for index, piece in pieces:
        projected = sum(len(part) + 2 for part in buffer) + len(piece)
        if buffer and projected > limit:
            _flush()
            if overlap_text and len(overlap_text) + len(piece) + 2 <= limit:
                buffer = [overlap_text]
                chunk_start = overlap_index
                chunk_end = overlap_index + 1
        if not buffer:
            chunk_start = index
            chunk_end = index + 1
        buffer.append(piece)
        chunk_end = index + 1
        overlap_text = piece
        overlap_index = index
    _flush()

    if not chunks:
        chunks.append({"chunk_text": "", "turn_start": start, "turn_end": start})

    total = len(chunks)
    return {
        "chunks": [
            {"chunk_index": idx + 1, "chunk_total": total, **chunk}
            for idx, chunk in enumerate(chunks)
        ],
        "total_turns": total_turns,
        "total_chars": len(raw),
        "compacted_chars": compacted_chars,
        "is_oversized": total > 1,
        "from_turn": start,
    }


def compute_content_hash(text: str) -> str:
    """The short fingerprint used by the idempotency / delta gate."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _path_is_within(raw_path: str, root: Path) -> bool:
    try:
        Path(raw_path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _scan_source_fields(source: Any) -> tuple[str, str, str]:
    """Read ``(client, path, glob)`` from a config model or a plain mapping.

    ``FlywheelConfig.model_validate`` yields ``ScanSourceConfig`` objects; a test
    or a script that assigns a raw dict onto the live config must still work.
    """
    if isinstance(source, dict):
        return (
            str(source.get("client") or "codex"),
            str(source.get("path") or ""),
            str(source.get("glob") or "**/*.jsonl"),
        )
    return (
        str(getattr(source, "client", "") or "codex"),
        str(getattr(source, "path", "") or ""),
        str(getattr(source, "glob", "") or "**/*.jsonl"),
    )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- the service


class DistillQueueService:
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
            },
            to_status=item.status.value,
            reason_code=reason_code,
        )

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
        return DistillQueueItem(
            queue_id=uuid.uuid4().hex,
            session_key=record.session_key,
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
    ) -> list[DistillQueueItem]:
        """A user/MCP declaration: bypass the noise filter and queue at P0.

        ``force`` (the default) clears an earlier ``opt_out`` because the user is
        now explicitly asking for this session to be distilled. Without it, an
        opted-out session is left untouched and nothing is enqueued.
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
        if record.opt_out and not force:
            return []
        record.opt_out = False
        record.opt_out_reason = ""
        record.status = SessionStatus.PENDING
        stored = self.db.upsert_session(record)

        text = self._session_text(stored)
        content_hash = compute_content_hash(text)
        total_turns = len(split_dialogue_turns(text))
        # Same content already queued: an explicit re-submission is idempotent.
        active = self.db.list_session_queue_items(stored.session_key, statuses=[QueueStatus.QUEUED])
        if (
            active
            and len(active) == active[0].chunk_total
            and all(item.content_hash == content_hash for item in active)
        ):
            return sorted(active, key=lambda item: item.chunk_index)

        self._supersede_queued_items(stored.session_key, actor=actor)
        revision = self.db.get_latest_queue_revision(stored.session_key) + 1
        prepared = prepare_queue_chunks(text, from_turn=0, max_item_chars=self.max_item_chars)
        items = self._build_chunk_items(
            stored,
            prepared,
            revision=revision,
            channel=channel,
            status=QueueStatus.QUEUED,
            priority=priority,
            content_hash=content_hash,
            focus_note=focus_note,
        )
        saved = self._insert_items(
            items, actor=actor, summary=f"explicit enqueue via {channel.value}"
        )
        self.db.update_session_watermark(
            stored.session_key,
            latest_content_hash=content_hash,
            latest_turn_count=total_turns,
        )
        return saved

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
    ) -> dict[str, Any]:
        """An archived conversation: opt-out veto, delta check, triage, then queue."""
        client_name = (client or "generic").strip() or "generic"
        sid = (session_id or "").strip()
        if not sid:
            return {"action": "skipped", "reason_code": "MISSING_SESSION_ID", "items": []}
        skey = f"{client_name}:{sid}"
        existing = self.db.get_session(skey)
        record = SessionRecord(
            session_key=skey,
            client=client_name,
            session_id=sid,
            title=(title or (existing.title if existing else "")) or f"{client_name} · {sid[:16]}",
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
            metadata=(
                dict(existing.metadata)
                if existing is not None and isinstance(existing.metadata, dict)
                else {}
            ),
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

        # Gate 3: deterministic triage rules (0-token pre-filter).
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
            if (
                item.status in {QueueStatus.QUEUED, QueueStatus.PROCESSING}
                and item.content_hash == content_hash
            ):
                return True
        newest_revision = items[0].revision
        newest = [item for item in items if item.revision == newest_revision]
        if not newest or any(item.content_hash != content_hash for item in newest):
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

    def re_triage_queue(
        self, *, dry_run: bool = True, actor: str = "portal"
    ) -> dict[str, Any]:
        """Re-evaluate the current triage rules over queued and rule-excluded items.

        ``dry_run=True`` computes the same change list with zero DB mutations.
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
        for item in candidates:
            if (
                item.status is QueueStatus.EXCLUDED
                and item.exclude_reason_code in RE_TRIAGE_SKIP_REASON_CODES
            ):
                continue
            session = self.db.get_session(item.session_key)
            if session is None or session.opt_out:
                continue
            result = triage_session(
                session,
                self.load_queue_item_text(item),
                self.cfg.sessions.triage,
                has_open_retro=item.session_key in open_retro_keys,
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
        return {
            "dry_run": bool(dry_run),
            "evaluated": len(candidates),
            "change_count": len(changes),
            "changes": changes,
        }

    def reconcile_codex_archives(self) -> dict[str, Any]:
        """Sync the configured Codex archive sources with the queue inventory.

        A file that appears (or grows) is archived in; a still-queued item whose
        archived file was removed is withdrawn automatically.
        """
        report: dict[str, Any] = {
            "sources": [],
            "scanned": 0,
            "enqueued": 0,
            "excluded": 0,
            "unchanged": 0,
            "withdrawn": 0,
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
                    result = self.on_archive(
                        src_client,
                        path.stem,
                        title=f"{src_client} archived session {path.stem[:16]}",
                        project="generic",
                        transcript_path=str(path),
                        channel=IngressChannel.CODEX_ARCHIVE,
                        actor="archive_sync",
                        source_path=str(path),
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

            for key in ("scanned", "enqueued", "excluded", "unchanged", "withdrawn"):
                report[key] += source_report[key]
            report["sources"].append(source_report)
        return report

    def bridge_pending_sessions_to_queue(
        self, *, limit: int = 500, actor: str = "p00_bridge"
    ) -> int:
        """Idempotently give legacy ``pending`` sessions a queue row (returns how many)."""
        created = 0
        for session in self.db.list_pending_sessions_prioritized(limit=max(1, int(limit))):
            if self.db.get_latest_queue_revision(session.session_key) > 0:
                continue
            items = self.on_explicit_enqueue(
                session,
                channel=IngressChannel.LEGACY_SCAN,
                actor=actor,
                force=False,
                priority=session.priority or "P1_GENERAL",
                focus_note="legacy pending session bridge",
            )
            created += len(items)
        return created

    # ---------------- read helpers ----------------

    def load_queue_item_text(self, item: DistillQueueItem) -> str:
        """The text of one queue item: its pre-compacted chunk, else the session text."""
        payload = item.payload if isinstance(item.payload, dict) else {}
        chunk_text = payload.get("chunk_text")
        if isinstance(chunk_text, str) and chunk_text.strip():
            return redact_text(chunk_text)
        session = self.db.get_session(item.session_key)
        if session is None:
            return ""
        return self._session_text(session)

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
    ) -> dict[str, Any]:
        """The MCP declaration surface: ``enqueue`` / ``opt_out`` / ``withdraw``."""
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




