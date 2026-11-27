from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.triage import (
    derive_readable_title,
    extract_jsonl_metadata,
    extract_jsonl_workspace,
    is_subagent_filename,
    triage_session,
)
from ai_flywheel.models import RegisterSessionRequest, SessionRecord, SessionStatus
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import redact_text


def read_transcript_excerpt(path: Path, max_chars: int = 24000) -> str:
    """Read local JSONL (Codex/Claude Code) or Markdown/text session transcript safely."""
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


def _extract_jsonl_text(obj: dict[str, Any]) -> str:
    # Support common Codex rollout / Claude Code transcript JSONL shapes,
    # strictly skipping developer/system prompt boilerplate and raw tool outputs
    # so triage, compaction and P02 see real human/assistant dialogue.
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


class SessionAdapter:
    """Dual-mode session ingestion: MCP-pushed content + local transcript file scan/read."""

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db

    def reconcile_scan_sources(self) -> dict[str, Any]:
        scanned = 0
        registered = 0
        filtered_out = 0
        gaps: list[str] = []
        triage_cfg = self.cfg.sessions.triage
        for src in self.cfg.sessions.scan_sources:
            root = self.cfg.resolve_path(src.path)
            if not root.exists():
                gaps.append(f"scan_source_missing:{src.client}:{src.path}")
                continue
            files = sorted(root.glob(src.glob), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
            for fp in files:
                if not fp.is_file():
                    continue
                scanned += 1
                meta = extract_jsonl_metadata(fp) if fp.suffix.lower() == ".jsonl" else {}
                is_sub = bool(meta.get("is_subagent"))
                parent_sid = meta.get("parent_session_id", "")
                parent_skey = f"{src.client}:{parent_sid}" if parent_sid else ""

                if triage_cfg.enabled and is_subagent_filename(fp.name, triage_cfg) and not parent_skey:
                    filtered_out += 1
                    continue

                sid = meta.get("canonical_session_id") or fp.stem
                skey = f"{src.client}:{sid}"
                if self.db.get_session(skey):
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
            "gaps": gaps,
        }

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
