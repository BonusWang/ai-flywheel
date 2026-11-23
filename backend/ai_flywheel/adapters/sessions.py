from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig
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


def _extract_jsonl_text(obj: dict[str, Any]) -> str:
    # Support common Codex rollout / Claude Code transcript JSONL shapes
    role = obj.get("role") or obj.get("type") or ""
    if isinstance(obj.get("message"), dict):
        msg = obj["message"]
        role = msg.get("role") or role
        content = msg.get("content")
        extracted = _flatten_content(content)
        if extracted:
            return f"[{role}] {extracted}"
    if "content" in obj:
        extracted = _flatten_content(obj.get("content"))
        if extracted:
            return f"[{role}] {extracted}"
    if isinstance(obj.get("payload"), dict):
        extracted = _flatten_content(obj["payload"].get("content") or obj["payload"].get("text"))
        if extracted:
            return f"[{role}] {extracted}"
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
        gaps: list[str] = []
        for src in self.cfg.sessions.scan_sources:
            root = self.cfg.resolve_path(src.path)
            if not root.exists():
                gaps.append(f"scan_source_missing:{src.client}:{src.path}")
                continue
            files = sorted(root.glob(src.glob), key=lambda p: p.stat().st_mtime, reverse=True)[:25]
            for fp in files:
                if not fp.is_file():
                    continue
                scanned += 1
                sid = fp.stem
                skey = f"{src.client}:{sid}"
                if self.db.get_session(skey):
                    continue
                rec = SessionRecord(
                    session_key=skey,
                    client=src.client,
                    session_id=sid,
                    title=f"{src.client} session {sid[:16]}",
                    transcript_path=str(fp),
                    priority="P1_GENERAL",
                    status=SessionStatus.PENDING,
                    metadata={"discovered_by": "p00_reconcile_scan"},
                )
                self.db.upsert_session(rec)
                registered += 1
        return {"scanned": scanned, "newly_registered": registered, "gaps": gaps}

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
