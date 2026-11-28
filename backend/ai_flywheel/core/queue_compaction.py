"""Pure transcript compaction, turn splitting, chunking, and structured-brief rendering for the distill queue.

Zero database or broker coupling: every function here is a deterministic transformation
from transcript text / structured brief into bounded queue chunks and content hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.sessions import read_transcript_excerpt
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
from ai_flywheel.storage.fs_store import redact_text

#: How a session is distilled: full (default), governance_only, or skipped.
DISTILL_MODE_FULL = "full"
DISTILL_MODE_GOVERNANCE_ONLY = "governance_only"
DISTILL_MODE_SKIPPED = "skipped"
DISTILL_MODES = frozenset(
    {DISTILL_MODE_FULL, DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}
)
HAND_DISTILLED_SKIP = "HAND_DISTILLED_SKIP"
HAND_DISTILLED_EVENT = "hand_distilled_declared"
HAND_DISTILLED_META_KEYS = (
    "distill_mode",
    "hand_distilled_paths",
    "hand_distilled_skip",
    "hand_distilled_at",
)

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


def read_full_transcript(path: Path, max_chars: int = MAX_FULL_TRANSCRIPT_CHARS) -> str:
    """Read a JSONL or plain-text transcript up to ``max_chars``, credentials masked.

    Delegates directly to :func:`ai_flywheel.adapters.sessions.read_transcript_excerpt`
    so the queue and the session adapter share one canonical JSONL transcript reader.
    """
    return read_transcript_excerpt(path, max_chars=max_chars)


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


