from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.triage import (
    antigravity_dialogue_text,
    derive_readable_title,
    extract_jsonl_metadata,
    extract_jsonl_workspace,
    is_antigravity_client,
    is_antigravity_record,
    is_subagent_filename,
    scan_quiet_window_seconds,
    transcript_is_active,
    triage_session,
)
from ai_flywheel.models import RegisterSessionRequest, SessionRecord, SessionStatus
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import redact_text


def read_transcript_excerpt(path: Path, max_chars: int = 24000) -> str:
    """Read local JSONL (Codex/Claude Code/Antigravity) or Markdown/text session transcript safely."""
    if not path.exists() or not path.is_file():
        return ""
    if path.suffix.lower() == ".jsonl":
        lines_out: list[str] = []
        total = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # A line that parses but is not a record — ``null``, ``[]``, ``123`` —
                    # has nothing to read, however valid it is as JSON.
                    if not isinstance(obj, dict):
                        continue
                    text = _extract_jsonl_text(obj)
                    if not text:
                        continue
                    cleaned = redact_text(text)
                    lines_out.append(cleaned)
                    total += len(cleaned)
                    if total >= max_chars:
                        break
        except OSError:
            return ""
        return "\n".join(lines_out)[:max_chars]

    try:
        return redact_text(path.read_text(encoding="utf-8", errors="ignore"))[:max_chars]
    except OSError:
        return ""


_BOILERPLATE_PREFIXES = (
    "<app-context>",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<system-reminder>",
    "# agents.md instructions",
    "# claude.md",
)


def _is_system_boilerplate(text: str) -> bool:
    stripped = text.strip().lower()
    return any(stripped.startswith(p) for p in _BOILERPLATE_PREFIXES)


_SKIPPED_RECORD_TYPES = frozenset(
    {
        "system",
        "developer",
        "tool_result",
        "tool_use",
        "function_call",
        "function_call_output",
        "reasoning",
        "thinking",
    }
)

#: How much of a pending session the triage sweep reads; a write dispatch found in an
#: excerpt this long is confirmed on the whole conversation.
_TRIAGE_EXCERPT_CHARS = 8000
_FULL_TRANSCRIPT_CHARS = 500_000


#: Session metadata that remembers a transcript as the scan last took it: its modification
#: time (epoch seconds) and its size at that moment. Antigravity has no archive event — its
#: transcript is a file that keeps growing under the same session id — so a file that differs
#: from either mark changed afterwards. Whether anything of the dialogue is new is the
#: queue's own judgement.
SCANNED_MTIME_KEY = "scanned_mtime"
SCANNED_SIZE_KEY = "scanned_size"


def _scan_marks(meta: dict[str, Any]) -> tuple[Any, Any]:
    """The marks of a transcript as the scan last took it: ``(modification time, size)``."""
    return (meta.get(SCANNED_MTIME_KEY), meta.get(SCANNED_SIZE_KEY))


def _is_scan_mark(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _extract_jsonl_text(obj: Any) -> str:
    # A line of a transcript is read by its keys. Whatever else a valid JSON line may
    # be — ``null``, a list, a number, a string — it is not a record and reads as nothing.
    if not isinstance(obj, dict):
        return ""
    # Support common Codex rollout / Claude Code transcript JSONL shapes,
    # strictly skipping developer/system prompt boilerplate and raw tool outputs
    # so triage, compaction and P02 see real human/assistant dialogue.
    # An Antigravity step is read by its step type: only the request and the answer
    # are dialogue, and no other step reaches the generic branches below.
    if is_antigravity_record(obj):
        return antigravity_dialogue_text(obj)
    role = str(obj.get("role") or obj.get("type") or "").strip().lower()
    if role in _SKIPPED_RECORD_TYPES:
        return ""
    if isinstance(obj.get("message"), dict):
        msg = obj["message"]
        mrole = str(msg.get("role") or role).strip().lower()
        if mrole in _SKIPPED_RECORD_TYPES:
            return ""
        content = msg.get("content")
        extracted = _flatten_content(content)
        if extracted and not _is_system_boilerplate(extracted):
            return f"[{mrole or 'message'}] {extracted}"
        return ""
    if "content" in obj:
        extracted = _flatten_content(obj.get("content"))
        if extracted and not _is_system_boilerplate(extracted):
            return f"[{role or 'message'}] {extracted}"
        return ""
    if isinstance(obj.get("payload"), dict):
        payload = obj["payload"]
        ptype = str(payload.get("type") or "").strip().lower()
        if ptype in _SKIPPED_RECORD_TYPES:
            return ""
        prole = str(payload.get("role") or "").strip().lower()
        if prole in _SKIPPED_RECORD_TYPES:
            return ""
        extracted = _flatten_content(payload.get("content") or payload.get("text"))
        if extracted and not _is_system_boilerplate(extracted):
            return f"[{prole or role or 'message'}] {extracted}"
    return ""


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                btype = str(item.get("type") or "").strip().lower()
                if btype in {"tool_result", "tool_use", "thinking", "reasoning", "image", "input_image"}:
                    continue
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return " ".join(parts).strip()
    return ""


def _is_same_file(left: str | None, right: Path) -> bool:
    """Whether a session's transcript path and a scanned file name the same file."""
    if not left:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


class SessionAdapter:
    """Dual-mode session ingestion: MCP-pushed content + local transcript file scan/read."""

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db
        #: Sessions whose registered transcript changed since the scan last took it, each with
        #: the ``(modification time, size)`` that was seen. The queue takes them with
        #: ``take_grown_transcripts``; until it has archived one, the next scan offers it again.
        self._grown: dict[str, tuple[float, int]] = {}

    def reconcile_scan_sources(self, now_ts: float | None = None) -> dict[str, Any]:
        """Register the sessions a passive scan of the configured sources finds.

        ``now_ts`` is the clock the Antigravity quiet window is measured against
        (epoch seconds; the wall clock when omitted). Its report keeps
        ``scanned`` / ``newly_registered`` / ``filtered_fragments`` / ``gaps`` and
        adds ``skipped_active``: transcripts still being written, left out of this
        pass and of ``scanned`` until they have gone quiet — and ``regrown``:
        transcripts the scan registered before that have grown since and gone
        quiet again. Those are handed to the queue (``take_grown_transcripts```)
        for the part of the conversation that is new.
        """
        scanned = 0
        registered = 0
        filtered_out = 0
        skipped_active = 0
        regrown = 0
        gaps: list[str] = []
        triage_cfg = self.cfg.sessions.triage
        quiet_seconds = scan_quiet_window_seconds(self.cfg)
        for src in self.cfg.sessions.scan_sources:
            root = self.cfg.resolve_path(src.path)
            if not root.exists():
                gaps.append(f"scan_source_missing:{src.client}:{src.path}")
                continue
            files = sorted(root.glob(src.glob), key=lambda p: p.stat().st_mtime, reverse=True)
            if quiet_seconds > 0 and is_antigravity_client(src.client):
                # Antigravity has no archive directory: a transcript is only finished once it
                # has stopped changing. One still being written is left out here, before the
                # newest-files cut — so it is neither registered nor given one of the slots —
                # and a later pass picks it up once it has gone quiet.
                quiet_files: list[Path] = []
                for fp in files:
                    if transcript_is_active(fp.stat().st_mtime, quiet_seconds=quiet_seconds, now_ts=now_ts):
                        skipped_active += 1
                    else:
                        quiet_files.append(fp)
                files = quiet_files
            files = files[:40]
            for fp in files:
                if not fp.is_file():
                    continue
                scanned += 1
                meta = extract_jsonl_metadata(fp, client=src.client) if fp.suffix.lower() == ".jsonl" else {}
                is_sub = bool(meta.get("is_subagent"))
                parent_sid = meta.get("parent_session_id", "")
                parent_skey = f"{src.client}:{parent_sid}" if parent_sid else ""

                if triage_cfg.enabled and is_subagent_filename(fp.name, triage_cfg) and not parent_skey:
                    filtered_out += 1
                    continue

                sid = meta.get("canonical_session_id") or fp.stem
                skey = f"{src.client}:{sid}"
                existing = self.db.get_session(skey)
                if existing is not None:
                    seen = self._grown_since_last_scan(existing, fp, client=src.client)
                    if seen is not None:
                        self._grown[skey] = seen
                        regrown += 1
                    continue
                ws = meta.get("workspace") or extract_jsonl_workspace(fp)
                title = derive_readable_title(
                    src.client,
                    sid,
                    custom_title=meta.get("custom_title", ""),
                    text=meta.get("first_user_message", ""),
                    workspace=ws,
                )
                rec_meta: dict[str, Any] = {"discovered_by": "p00_reconcile_scan"}
                if is_antigravity_client(src.client):
                    stat = fp.stat()
                    rec_meta[SCANNED_MTIME_KEY] = stat.st_mtime
                    rec_meta[SCANNED_SIZE_KEY] = stat.st_size
                if parent_skey:
                    rec_meta["parent_session_key"] = parent_skey
                if is_sub:
                    rec_meta["is_subagent"] = True
                if meta.get("subagent_nickname"):
                    rec_meta["subagent_nickname"] = meta["subagent_nickname"]
                if meta.get("subagent_role"):
                    rec_meta["subagent_role"] = meta["subagent_role"]
                rec = SessionRecord(
                    session_key=skey,
                    client=src.client,
                    session_id=sid,
                    title=title,
                    workspace=ws,
                    transcript_path=str(fp),
                    priority="P1_GENERAL",
                    status=SessionStatus.PENDING,
                    metadata=rec_meta,
                )
                self.db.upsert_session(rec)
                registered += 1
        return {
            "scanned": scanned,
            "newly_registered": registered,
            "filtered_fragments": filtered_out,
            "skipped_active": skipped_active,
            "regrown": regrown,
            "gaps": gaps,
        }

    def _grown_since_last_scan(
        self, session: SessionRecord, path: Path, *, client: str
    ) -> tuple[float, int] | None:
        """``(modification time, size)`` of ``path`` when it changed since the scan last took it.

        Antigravity has no archive directory and no archive event: a transcript is a file that
        keeps growing under the same session id, and a pass archives it once it has gone quiet.
        Without a mark of that look, the conversation could continue for the rest of the day and
        nothing after the first archive would ever reach the queue. The marks are the file's
        modification time (``scanned_mtime``) and size (``scanned_size``) as they were then; a
        file that differs from either changed afterwards. The time alone would miss a file
        written under a clock that went back, or one that kept its time and not its content.
        The caller has already seen that it is quiet again, and whether anything of the dialogue
        is new is the queue's own delta judgement.

        Only a session this scan registered, read from the very file it was registered from, is
        looked at: a session registered another way keeps being left alone as before. A session
        registered before the marks existed starts counting from now. A session frozen by an
        opt-out is not looked at: there is nothing in it the queue may take.
        """
        if not is_antigravity_client(client) or session.opt_out:
            return None
        meta = session.metadata if isinstance(session.metadata, dict) else {}
        if meta.get("discovered_by") != "p00_reconcile_scan" or not _is_same_file(
            session.transcript_path, path
        ):
            return None
        stat = path.stat()
        seen = (stat.st_mtime, stat.st_size)
        if not all(_is_scan_mark(mark) for mark in _scan_marks(meta)):
            self.mark_transcript_scanned(session.session_key, *seen)
            return None
        return None if seen == _scan_marks(meta) else seen

    def take_grown_transcripts(self) -> list[tuple[str, float, int]]:
        """``(session_key, modification time, size)`` of every transcript found changed and not
        yet handed over, and forget them: the queue archives each as it was at that time."""
        grown = sorted(self._grown.items(), key=lambda entry: (entry[1], entry[0]))
        self._grown.clear()
        return [(session_key, *seen) for session_key, seen in grown]

    def mark_transcript_scanned(self, session_key: str, mtime: float, size: int) -> None:
        """Record that the transcript of ``session_key`` was taken as it was at ``mtime``.

        Nothing else about the session changes — not its lifecycle, nor an opt-out.
        """
        session = self.db.get_session(session_key)
        if session is None:
            return
        meta = session.metadata if isinstance(session.metadata, dict) else {}
        if _scan_marks(meta) == (mtime, size):
            return
        session.metadata = {**meta, SCANNED_MTIME_KEY: mtime, SCANNED_SIZE_KEY: size}
        self.db.upsert_session(session, preserve_existing_lifecycle=True)

    def sweep_pending_triage(self, open_retro_session_keys: set[str] | None = None) -> dict[str, Any]:
        """Run Level-1 0-token triage over all pending sessions; archive obvious temp/noise chats."""
        triage_cfg = self.cfg.sessions.triage
        if not triage_cfg.enabled:
            return {"checked": 0, "admitted": 0, "skipped": 0, "reasons": {}}

        retro_keys = open_retro_session_keys or set()
        pending = self.db.list_sessions(status=SessionStatus.PENDING.value, limit=200)
        admitted = 0
        skipped = 0
        reason_counts: dict[str, int] = {}

        for sess in pending:
            text = self.load_session_text(sess, max_chars=_TRIAGE_EXCERPT_CHARS)
            res = triage_session(
                sess,
                text,
                triage_cfg,
                has_open_retro=(sess.session_key in retro_keys),
            )
            if res.get("reason") == "controlled_write_dispatch" and len(text) >= _TRIAGE_EXCERPT_CHARS:
                # The excerpt may end before a later question. Whether anything follows
                # the instruction — what makes a session a bare write dispatch — is
                # decided on the whole conversation.
                res = triage_session(
                    sess,
                    self.load_session_text(sess, max_chars=_FULL_TRANSCRIPT_CHARS),
                    triage_cfg,
                    has_open_retro=(sess.session_key in retro_keys),
                )
            discovered_ws = res.get("workspace") or ""
            changed = False
            if discovered_ws and not sess.workspace:
                sess.workspace = discovered_ws
                changed = True
            meta = dict(sess.metadata) if isinstance(sess.metadata, dict) else {}
            if not res["admit"]:
                sess.status = SessionStatus.ARCHIVED
                meta["triage_status"] = "skipped"
                meta["triage_reason"] = res["reason"]
                sess.metadata = meta
                self.db.upsert_session(sess)
                skipped += 1
                bucket = res["reason"].split(" ")[0]
                reason_counts[bucket] = reason_counts.get(bucket, 0) + 1
            else:
                admitted += 1
                if meta.get("triage_tier") != res["tier"] or changed:
                    meta["triage_status"] = "admitted"
                    meta["triage_tier"] = res["tier"]
                    meta["triage_reason"] = res["reason"]
                    sess.metadata = meta
                    self.db.upsert_session(sess)

        return {
            "checked": len(pending),
            "admitted": admitted,
            "skipped": skipped,
            "reasons": reason_counts,
        }

    def load_session_text(self, session: SessionRecord, max_chars: int = 24000) -> str:
        chunks: list[str] = []
        if session.title:
            chunks.append(f"Title: {session.title}")
        if session.summary:
            chunks.append(f"Summary: {redact_text(session.summary)}")
        if session.content:
            chunks.append(redact_text(session.content))
        elif session.transcript_path:
            p = Path(session.transcript_path)
            excerpt = read_transcript_excerpt(p, max_chars=max_chars)
            if excerpt:
                chunks.append(excerpt)
        return "\n\n".join(chunks)[:max_chars]

    def classify_priority(self, req: RegisterSessionRequest, has_open_retro: bool = False) -> str:
        if has_open_retro:
            return "P1_SIGNAL"
        lower_tags = {t.lower() for t in req.tags}
        if lower_tags & {"core", "prod", "delivery", "bug", "incident"}:
            return "P0_CORE"
        if lower_tags & {"meta", "debug-tool", "config-test"}:
            return "P2_META"
        return "P1_GENERAL"
