"""The distill-queue state machine: four ingress events, three gates, one trail.

Every way a session can enter the queue converges here — Codex archive
reconciliation, the Claude Code ``SessionEnd`` hook, an MCP declaration, and a
manual portal submission — so the queue behaves identically whatever the entry
point. Each transition writes the structured ``audit_events`` trail
(``entity_type="queue_item"`` with ``from_status`` / ``to_status`` /
``reason_code``) and publishes ``queue_updated`` on the event broker.

Oversized conversations are handled deterministically and without a model call:
the transcript is read in full (never truncated at the 24k review limit) and split
into dialogue turns. A dialogue that fits one queue item is kept word for word;
one that does not is compacted turn by turn first (the tail of a long tool output
is kept, so conclusions survive) and only then split into ordered chunks
``1/N .. N/N``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_flywheel.adapters.sessions import _extract_jsonl_text
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

#: The archive/transcript reader is allowed to look at far more than one queue
#: item: truncating before compaction would silently drop the tail conclusions.
MAX_FULL_TRANSCRIPT_CHARS = 500_000

#: A long tool output / log block keeps its head and its tail; the middle folds. A turn is
#: folded only when the whole dialogue will not fit one queue item (``prepare_queue_chunks``):
#: one that fits is queued word for word, however long a single turn of it is.
MAX_COMPACT_TURN_CHARS = 2500
COMPACT_HEAD_CHARS = 1200
COMPACT_TAIL_CHARS = 800
#: User turns are preserved generously, but huge pasted logs still fold in the middle.
MAX_USER_TURN_CHARS = 6000
USER_COMPACT_HEAD_CHARS = 2500
USER_COMPACT_TAIL_CHARS = 1500
#: Hard upper bound on chunks per single session so one giant log never floods the queue.
MAX_CHUNKS_PER_SESSION = 4

#: One reconciliation pass looks at the newest files of a source; the scan is a
#: lightweight poll, not a full-history import.
MAX_ARCHIVE_FILES_PER_SOURCE = 500

#: Excluded items with these codes are user/system decisions and are never
#: re-opened by a rules re-triage.
RE_TRIAGE_SKIP_REASON_CODES = frozenset(
    {"USER_OPT_OUT", "MANUAL_EXCLUDE", "SUPERSEDED", "HAND_DISTILLED_SKIP"}
)

#: How a session is distilled. ``full`` is the default: review it and write. A
#: session whose knowledge the client has already written up by hand is declared
#: ``governance_only`` (rules, mistakes and dissents, never a knowledge document)
#: or ``skipped`` (nothing at all). The mode lives on the queue item payload —
#: the durable record — and is mirrored into the session metadata.
DISTILL_MODE_FULL = "full"
DISTILL_MODE_GOVERNANCE_ONLY = "governance_only"
DISTILL_MODE_SKIPPED = "skipped"
DISTILL_MODES = frozenset(
    {DISTILL_MODE_FULL, DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}
)

#: Exclude reason of a session the client hand-distilled and took out entirely.
HAND_DISTILLED_SKIP = "HAND_DISTILLED_SKIP"
#: Audit trail of every queue item a ``hand_distilled`` declaration touched.
HAND_DISTILLED_EVENT = "hand_distilled_declared"

#: How much of a session's opening a re-triage reads to recognise a write dispatch. A
#: verdict taken on an excerpt this long is confirmed on the whole conversation.
MAX_OPENING_CHARS = 24000
_OPENING_USER_TURN_RE = re.compile(r"^\[(?:user|human)\]", re.IGNORECASE | re.MULTILINE)

#: Session metadata keys that mirror a hand-distilled declaration.
HAND_DISTILLED_META_KEYS = (
    "distill_mode",
    "hand_distilled_paths",
    "hand_distilled_skip",
    "hand_distilled_at",
)

#: Why a passive pass archived nothing: the transcript was still being written. Antigravity
#: has no archive directory, so its transcripts are left alone until they have gone quiet.
#: The pass answers ``skipped`` with this code and touches neither session nor queue.
QUIET_WINDOW_ACTIVE = "QUIET_WINDOW_ACTIVE"


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

    Called for a dialogue that has to be split (see ``prepare_queue_chunks``); a
    turn is never folded on its own while the dialogue still fits one queue item.

    Ordinary ``[user]`` turns are kept verbatim up to ``MAX_USER_TURN_CHARS``;
    massive pasted logs inside a user turn still fold in the middle while
    keeping head and tail intact. Long assistant/tool turns fold above
    ``max_turn_chars``.
    """
    text = (turn or "").strip()
    if len(text) <= max_turn_chars:
        return text
    role = _role_of(text)
    if role in {"user", "human", ""}:
        if len(text) <= MAX_USER_TURN_CHARS:
            return text
        head = text[:USER_COMPACT_HEAD_CHARS]
        tail = text[-USER_COMPACT_TAIL_CHARS:]
        omitted = len(text) - USER_COMPACT_HEAD_CHARS - USER_COMPACT_TAIL_CHARS
        label = role or "text"
        marker = f"[flywheel: {omitted} chars compacted from the middle of this {label} turn]"
        return f"{head}\n\n{marker}\n\n{tail}"
    head = text[:COMPACT_HEAD_CHARS]
    tail = text[-COMPACT_TAIL_CHARS:]
    omitted = len(text) - COMPACT_HEAD_CHARS - COMPACT_TAIL_CHARS
    marker = f"[flywheel: {omitted} chars compacted from the middle of this {role} turn]"
    return f"{head}\n\n{marker}\n\n{tail}"


def _cap_chunks_preserving_head_and_tail(
    chunks: list[dict[str, Any]], max_chunks: int
) -> list[dict[str, Any]]:
    """Bound chunks per session while always retaining opening context and final conclusions."""
    cap = max(1, int(max_chunks))
    if len(chunks) <= cap:
        return chunks
    if cap == 1:
        return [chunks[-1]]
    if cap == 2:
        return [chunks[0], chunks[-1]]
    # Always keep index 0 (opening) and index -1 (tail conclusion), plus evenly spaced middle chunks.
    middle_slots = cap - 2
    span = len(chunks) - 2
    picked_middle: list[dict[str, Any]] = []
    for slot in range(middle_slots):
        idx = 1 + int(round((slot + 1) * span / (middle_slots + 1))) - 1
        idx = max(1, min(len(chunks) - 2, idx))
        if chunks[idx] not in picked_middle:
            picked_middle.append(chunks[idx])
    return [chunks[0], *picked_middle, chunks[-1]]


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
    max_chunks: int = MAX_CHUNKS_PER_SESSION,
) -> dict[str, Any]:
    """Keep the dialogue whole, and only fold and pack it when it will not fit one item.

    ``from_turn`` slices the delta when the session was already consumed up to a
    watermark (``0 < from_turn < len(turns)``). Each chunk carries its
    ``chunk_text`` together with the global ``turn_start`` / ``turn_end``
    boundaries, and adjacent chunks share the last turn as overlap context.

    A dialogue that fits ``max_item_chars`` is queued word for word, however long
    one of its turns is: folding a turn is for a dialogue that has to be split —
    the middle of a long answer is exactly where a root cause or a fix is explained.
    Only past that size are long turns folded in the middle (``compact_turn_text``)
    and the result packed into ordered chunks.
    """
    raw = text or ""
    turns = split_dialogue_turns(raw)
    total_turns = len(turns)
    limit = max(1000, int(max_item_chars))
    requested = int(from_turn or 0)
    start = requested if 0 < requested < total_turns else 0

    verbatim = "\n\n".join(turns[start:])
    if len(verbatim) <= limit:
        return {
            "chunks": [
                {
                    "chunk_index": 1,
                    "chunk_total": 1,
                    "chunk_text": verbatim,
                    "turn_start": start,
                    "turn_end": total_turns,
                }
            ],
            "total_turns": total_turns,
            "total_chars": len(raw),
            "compacted_chars": len(verbatim),
            "is_oversized": False,
            "omitted_middle_chunks": 0,
            "from_turn": start,
        }

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

    raw_chunk_count = len(chunks)
    if raw_chunk_count > max_chunks:
        chunks = _cap_chunks_preserving_head_and_tail(chunks, max_chunks)

    total = len(chunks)
    return {
        "chunks": [
            {"chunk_index": idx + 1, "chunk_total": total, **chunk}
            for idx, chunk in enumerate(chunks)
        ],
        "total_turns": total_turns,
        "total_chars": len(raw),
        "compacted_chars": compacted_chars,
        "is_oversized": total > 1 or raw_chunk_count > total,
        "omitted_middle_chunks": max(0, raw_chunk_count - total),
        "from_turn": start,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_content_hash(
    text: str, *, structured_brief: dict[str, Any] | None = None
) -> str:
    """The short fingerprint used by the idempotency / delta gate.

    A structured brief is part of what was submitted, so it is part of the
    fingerprint: the same transcript sent with a new brief is a new revision, and
    the same brief sent twice is the same submission. Without a brief the
    fingerprint is exactly what it always was.
    """
    digest = hashlib.sha256((text or "").encode("utf-8"))
    if structured_brief:
        digest.update(b"\n\x00structured_brief\n")
        digest.update(_canonical_json(structured_brief).encode("utf-8"))
    return digest.hexdigest()[:16]


def render_structured_brief(brief: Any) -> str:
    """The brief as the review reads it, after the dialogue it belongs to.

    Every line is an indented list entry, so nothing a client wrote in the brief
    can read as a dialogue role marker.
    """
    if not isinstance(brief, dict) or not brief:
        return ""
    lines = [
        "flywheel-brief: structured brief submitted by the client "
        "(evidence for the review, not dialogue)"
    ]
    for key, value in brief.items():
        for entry in value if isinstance(value, list) else [value]:
            text = entry if isinstance(entry, str) else _canonical_json(entry)
            for line in f"{key}: {text}".splitlines() or [f"{key}: "]:
                lines.append(f"  - {line}".rstrip())
    return "\n".join(lines)


def redact_hand_distilled_paths(raw_paths: Any) -> list[str]:
    """The knowledge paths a client reports as hand-written: redacted, trimmed, deduplicated."""
    paths: list[str] = []
    for raw in raw_paths if isinstance(raw_paths, (list, tuple)) else []:
        path = redact_text(str(raw)).strip()[:500]
        if path and path not in paths:
            paths.append(path)
    return paths


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
        from ai_flywheel.core.triage import _is_explicit_submission

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




