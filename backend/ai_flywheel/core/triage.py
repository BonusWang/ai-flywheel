from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_flywheel.config import SessionTriageConfig
from ai_flywheel.models import SessionRecord

_KNOWLEDGE_OR_PROJECT_SIGNALS = (
    "架构",
    "设计",
    "蓝图",
    "方案",
    "调研",
    "对比",
    "复盘",
    "排查",
    "根因",
    "报错",
    "异常",
    "修复",
    "优化",
    "规范",
    "契约",
    "沉淀",
    "知识库",
    "经验卡",
    "工作流",
    "发布",
    "部署",
    "回滚",
    "数据库",
    "索引",
    "性能",
    "测试",
    "验收",
    "skill",
    "wiki",
    "mcp",
    "sql",
    "api",
    "bug",
    "fix",
    "error",
    "exception",
    "traceback",
    "deploy",
    "migration",
    "schema",
    "refactor",
    "architecture",
    "playbook",
)

_TURN_MARKER_RE = re.compile(r"^\[(?:user|assistant|human|model)\]", re.IGNORECASE | re.MULTILINE)

#: Tags that veto any distillation for the session they mark.
OPT_OUT_TAGS = frozenset({"no-flywheel", "no-distill", "opt-out"})

#: Text markers a user can type inside the conversation itself. Matching is
#: case-insensitive, so `#No-Flywheel` and `#NO-DISTILL` are caught too.
OPT_OUT_TEXT_MARKERS = (
    "#no-flywheel",
    "#no-distill",
    "本会话不沉淀",
    "不要沉淀",
    "不参与沉淀",
)

#: Triage reasons that mean "admitted": they map to the empty reason code.
_ADMIT_REASON_MARKERS = (
    "triage_disabled",
    "explicit_user_submission",
    "project_or_knowledge",
    "open_retro_signal",
    "deep_knowledge_chat",
    "general_workspace_qualified",
)

#: Exclude reason prefix -> standard dictionary code (``exclude_reason`` type).
_EXCLUDE_REASON_CODE_MAP: tuple[tuple[str, str], ...] = (
    ("user_opt_out", "USER_OPT_OUT"),
    ("opt_out", "USER_OPT_OUT"),
    ("subagent", "SUBAGENT_FRAGMENT"),
    ("fragment", "SUBAGENT_FRAGMENT"),
    ("excluded_workspace", "EXCLUDED_WORKSPACE"),
    ("too_short", "TOO_SHORT"),
    ("temp_chat", "TEMP_CHAT_LOW_SIGNAL"),
    ("low_signal", "TEMP_CHAT_LOW_SIGNAL"),
    ("single_turn", "SINGLE_TURN_LOOKUP"),
    ("manual", "MANUAL_EXCLUDE"),
)


def has_explicit_opt_out(
    text: str,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Whether the session carries an explicit "do not distill me" decision.

    Three sources are checked, in the order the queue gate evaluates them:
    a profile metadata flag (``opt_out: true``), an opt-out tag
    (``no-flywheel`` / ``no-distill`` / ``opt-out``), and a marker typed inside
    the conversation itself (``#no-flywheel`` / ``本会话不沉淀`` ...).
    """
    if isinstance(metadata, dict) and metadata.get("opt_out") is True:
        return True
    for raw in tags or []:
        tag = str(raw).strip().lower().lstrip("#")
        if tag in OPT_OUT_TAGS:
            return True
    lowered = (text or "").lower()
    return any(marker in lowered for marker in OPT_OUT_TEXT_MARKERS)


def map_triage_reason_code(reason: str) -> str:
    """Map a free-form triage reason onto a standard ``exclude_reason`` code.

    An admitted reason (or an empty one) maps to ``""``; an unrecognised
    exclude reason falls back to ``MANUAL_EXCLUDE`` so the console always has a
    labelled bucket instead of a raw string.
    """
    lowered = str(reason or "").strip().lower()
    if not lowered:
        return ""
    if any(marker in lowered for marker in _ADMIT_REASON_MARKERS):
        return ""
    for marker, code in _EXCLUDE_REASON_CODE_MAP:
        if marker in lowered:
            return code
    return "MANUAL_EXCLUDE"



def extract_jsonl_workspace(path: Path, max_lines: int = 25) -> str:
    """Inspect the opening lines of a Codex/Claude Code JSONL transcript to discover its workspace cwd."""
    if not path.exists() or not path.is_file() or path.suffix.lower() != ".jsonl":
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for idx, raw in enumerate(f):
                if idx >= max_lines:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                for key in ("cwd", "workspace", "project_root"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                payload = obj.get("payload")
                if isinstance(payload, dict):
                    for key in ("cwd", "workspace", "project_root"):
                        val = payload.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
    except OSError:
        return ""
    return ""


def is_subagent_filename(name: str, cfg: SessionTriageConfig) -> bool:
    lower = name.strip().lower()
    return any(lower.startswith(prefix.lower()) for prefix in cfg.exclude_filename_prefixes)


def _match_any_pattern(text: str, patterns: list[str]) -> bool:
    lower = text.replace("\\", "/").lower()
    if not lower:
        return False
    segments = [seg for seg in re.split(r"[/:\s]+", lower) if seg]
    for pat in patterns:
        p = pat.strip().lower()
        if not p:
            continue
        if p in lower or any(p in seg for seg in segments):
            return True
    return False


def count_effective_turns(text: str) -> int:
    markers = _TURN_MARKER_RE.findall(text)
    if markers:
        return len(markers)
    # Fallback: count substantial non-header paragraphs
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= 30]
    return len(paras)


def _is_explicit_submission(session: SessionRecord) -> bool:
    meta = session.metadata if isinstance(session.metadata, dict) else {}
    if meta.get("explicit_ingest") is True:
        return True
    if "manual" in {t.lower() for t in (session.tags or [])}:
        return True
    # Direct MCP ingest_session / manual registration pushes inline content (not just a transcript path)
    if session.content and session.content.strip() and meta.get("discovered_by") != "p00_reconcile_scan":
        return True
    return False


def triage_session(
    session: SessionRecord,
    text: str,
    cfg: SessionTriageConfig,
    *,
    has_open_retro: bool = False,
) -> dict[str, Any]:
    """Level-1 deterministic session triage (0-token pre-filter before P02 LLM review).

    ``reason_code`` is the standard ``exclude_reason`` code for a rejected
    session (``USER_OPT_OUT`` / ``SUBAGENT_FRAGMENT`` / ...) and the empty
    string for an admitted one; ``reason`` keeps the human-readable detail.
    """
    if not cfg.enabled:
        return {
            "admit": True,
            "tier": "triage_disabled",
            "reason": "triage_disabled",
            "reason_code": "",
            "workspace": session.workspace,
        }

    # 1. Explicit user ingestion (MCP ingest_session / manual UI) always bypasses auto-drop
    if _is_explicit_submission(session):
        return {
            "admit": True,
            "tier": "explicit",
            "reason": "explicit_user_submission",
            "reason_code": "",
            "workspace": session.workspace,
        }

    # 2. Subagent or system fragment check (by session_id or transcript filename)
    tp_name = Path(session.transcript_path).name if session.transcript_path else ""
    if is_subagent_filename(session.session_id, cfg) or (tp_name and is_subagent_filename(tp_name, cfg)):
        return {
            "admit": False,
            "tier": "skipped",
            "reason": "subagent_or_system_fragment",
            "reason_code": map_triage_reason_code("subagent_or_system_fragment"),
            "workspace": session.workspace,
        }

    # 3. Discover workspace if empty
    workspace = (session.workspace or "").strip()
    if not workspace and session.transcript_path:
        workspace = extract_jsonl_workspace(Path(session.transcript_path))

    # 4. Excluded workspace blacklist
    if workspace and _match_any_pattern(workspace, cfg.exclude_workspace_patterns):
        return {
            "admit": False,
            "tier": "skipped",
            "reason": "excluded_workspace",
            "reason_code": map_triage_reason_code("excluded_workspace"),
            "workspace": workspace,
        }

    # 5. Strip boilerplate header lines before measuring real body length
    body_text = re.sub(r"^(?:Title|Summary):.*$", "", text or "", flags=re.MULTILINE).strip()
    char_len = len(body_text)
    if char_len < cfg.min_chars and not has_open_retro:
        reason = f"too_short_or_empty ({char_len}<{cfg.min_chars})"
        return {
            "admit": False,
            "tier": "skipped",
            "reason": reason,
            "reason_code": map_triage_reason_code(reason),
            "workspace": workspace,
        }

    # 6. Formal project or knowledge workspace -> admit directly for P02 review
    project_non_generic = (session.project or "").strip().lower() not in {"", "generic", "general"}
    is_temp_ws = bool(workspace) and _match_any_pattern(workspace, cfg.temp_workspace_patterns)
    is_project_ws = bool(workspace) and _match_any_pattern(workspace, cfg.project_workspace_patterns)

    if (is_project_ws and not is_temp_ws) or project_non_generic:
        return {
            "admit": True,
            "tier": "project_or_knowledge",
            "reason": "project_or_knowledge_workspace",
            "reason_code": "",
            "workspace": workspace,
        }

    # 7. Gray-list (*Chat / temporary directory / unknown empty workspace):
    # Require deeper content (>= gray_workspace_min_chars) AND substantive knowledge/project signals, or an open retro signal.
    lower_body = f"{session.title} {session.summary or ''} {body_text[:6000]}".lower()
    matched_signals = [kw for kw in _KNOWLEDGE_OR_PROJECT_SIGNALS if kw in lower_body]
    turns = count_effective_turns(body_text)

    if has_open_retro:
        return {
            "admit": True,
            "tier": "gray_qualified",
            "reason": "open_retro_signal",
            "reason_code": "",
            "workspace": workspace,
        }

    if is_temp_ws or not workspace:
        if char_len >= cfg.gray_workspace_min_chars and len(matched_signals) >= 2 and turns >= cfg.min_turns:
            return {
                "admit": True,
                "tier": "gray_qualified",
                "reason": f"deep_knowledge_chat (chars={char_len}, signals={','.join(matched_signals[:3])})",
                "reason_code": "",
                "workspace": workspace,
            }
        reason = f"temp_chat_low_signal (chars={char_len}, signals={len(matched_signals)}, turns={turns})"
        return {
            "admit": False,
            "tier": "skipped",
            "reason": reason,
            "reason_code": map_triage_reason_code(reason),
            "workspace": workspace,
        }

    # 8. Other non-temp workspace with sufficient depth
    if turns < cfg.min_turns and char_len < cfg.gray_workspace_min_chars:
        reason = f"single_turn_lookup (turns={turns}, chars={char_len})"
        return {
            "admit": False,
            "tier": "skipped",
            "reason": reason,
            "reason_code": map_triage_reason_code(reason),
            "workspace": workspace,
        }

    return {
        "admit": True,
        "tier": "project_or_knowledge",
        "reason": "general_workspace_qualified",
        "reason_code": "",
        "workspace": workspace,
    }
