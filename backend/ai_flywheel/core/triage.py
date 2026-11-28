from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ai_flywheel.config import SessionTriageConfig
from ai_flywheel.adapters.transcripts import (
    DEFAULT_SCAN_QUIET_WINDOW_SECONDS,
    _UUID_RE,
    _is_boilerplate_user_text,
    antigravity_dialogue_text,
    antigravity_session_id,
    antigravity_workspace,
    clean_user_message,
    derive_readable_title,
    extract_first_user_message,
    extract_jsonl_metadata,
    extract_jsonl_workspace,
    flatten_content,
    is_antigravity_client,
    is_antigravity_record,
    is_boilerplate_user_text,
    is_subagent_filename,
    scan_quiet_window_seconds,
    strip_antigravity_wrappers,
    transcript_is_active,
)
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
    "subagent_attached",
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




_NO_TOOL_BATCH_MARKERS = (
    "all tools disabled",
    "所有工具已关闭",
    "严禁调用任何工具",
    "所有工具均已禁用",
    "tools are disabled",
    "禁用所有工具",
)


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
    if meta.get("discovered_by") in {"p00_reconcile_scan", "on_archive", "archive"}:
        return False
    # Direct MCP ingest_session / manual registration pushes inline content (not just a transcript path)
    if session.content and session.content.strip():
        return True
    return False


_USER_TURN_LINE_RE = re.compile(r"^\[(?:user|human)\]", re.IGNORECASE | re.MULTILINE)
_NEXT_ROLE_MARKER_RE = re.compile(r"\[(?:assistant|model|user|human)\]", re.IGNORECASE)
_MD_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.IGNORECASE | re.MULTILINE)
_MD_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+\S", re.MULTILINE)

#: A controlled write is an *approved* write: a dispatch hands over a write-set that was
#: approved and names the write contract it has to pass. Both are tool-neutral on purpose;
#: a phrase only one installation's dispatches use is configured, never coded
#: (``sessions.triage.controlled_write_markers``).
_CONTROLLED_WRITE_APPROVAL_RE = re.compile(
    r"(?:批准|授权|审批|已批|获批|approved?|authori[sz]ed)[^。\n]{0,30}write[-_ ]set"
    r"|write[-_ ]set[^。\n]{0,16}(?:批准|授权|审批|获批|approved?|authori[sz]ed)",
    re.IGNORECASE,
)
_CONTROLLED_WRITE_CONTRACT_MARKERS = (
    "受控写入",
    "受控沉淀",
    "写前门禁",
    "写后核验",
    "写后钩子",
    "postflight",
    "preimage",
    "postimage",
)

#: A dispatch is a written brief, not a remark that happens to name an approved write-set:
#: it is long and laid out in sections or steps. Below this, a conversation that merely
#: talks *about* a controlled write is left alone.
_CONTROLLED_WRITE_MIN_OPENING_CHARS = 1200
_CONTROLLED_WRITE_MIN_HEADINGS = 2
_CONTROLLED_WRITE_MIN_LIST_ITEMS = 6

#: A user turn that only carries the machine's own bookkeeping is not a question a person
#: asked: a context continuation, an injected reminder, a hook's feedback, or a message an
#: orchestrator relayed to an agent that was already running.
_NON_DIALOGUE_USER_PREFIXES = (
    "this session is being continued from a previous conversation",
    "<system-reminder>",
    "<task-notification>",
    "the coordinator sent a message",
)


def _dialogue_user_turns(text: str) -> list[str]:
    """The user turns that carry an instruction or a question, in order."""
    turns: list[str] = []
    for match in _USER_TURN_LINE_RE.finditer(text or ""):
        rest = text[match.end():].lstrip()
        if any(rest.lower().startswith(prefix) for prefix in _NON_DIALOGUE_USER_PREFIXES):
            continue
        end = _NEXT_ROLE_MARKER_RE.search(rest)
        turns.append(rest[: end.start()].strip() if end else rest.strip())
    return turns


def _configured_write_markers(cfg: SessionTriageConfig | None) -> tuple[str, ...]:
    raw = getattr(cfg, "controlled_write_markers", None) if cfg is not None else None
    return tuple(str(marker).strip().lower() for marker in (raw or []) if str(marker).strip())


def _is_dispatch_brief(opening: str) -> bool:
    """Whether an opening instruction is laid out like a written brief."""
    if len(opening) < _CONTROLLED_WRITE_MIN_OPENING_CHARS:
        return False
    return (
        len(_MD_HEADING_LINE_RE.findall(opening)) >= _CONTROLLED_WRITE_MIN_HEADINGS
        or len(_MD_LIST_LINE_RE.findall(opening)) >= _CONTROLLED_WRITE_MIN_LIST_ITEMS
    )


def is_controlled_write_dispatch(text: str, cfg: SessionTriageConfig | None = None) -> bool:
    """Whether a transcript is the *dispatch* of a controlled knowledge-base write.

    Such a session is the writing of a document — one instruction, then the writer
    working through it — not knowledge about the work behind it. Distilled, it would
    describe the writing process to the very knowledge base it wrote to.

    It is recognised by content, never by a tool or agent name, and it has to be
    unambiguous, because the cost of being wrong is a real conversation never being
    distilled:

    * the conversation has exactly one instruction and nothing follows it — a message
      an orchestrator relayed while the writer was running does not count;
    * the instruction hands over an *approved* write-set and names the write contract,
      or carries a marker configured in ``sessions.triage.controlled_write_markers``;
    * unless a configured marker says so, it is a written brief — long, laid out in
      sections or steps — so that a question *about* a controlled write is never taken
      for one.
    """
    turns = _dialogue_user_turns(text)
    if len(turns) != 1:
        return False
    opening = turns[0].lower()
    if any(marker in opening for marker in _configured_write_markers(cfg)):
        return True
    if not _CONTROLLED_WRITE_APPROVAL_RE.search(opening):
        return False
    if not any(marker in opening for marker in _CONTROLLED_WRITE_CONTRACT_MARKERS):
        return False
    return _is_dispatch_brief(opening)


def triage_session(
    session: SessionRecord,
    text: str,
    cfg: SessionTriageConfig,
    *,
    has_open_retro: bool = False,
    opening_text: str = "",
) -> dict[str, Any]:
    """Level-1 deterministic session triage (0-token pre-filter before P02 LLM review).

    ``reason_code`` is the standard ``exclude_reason`` code for a rejected
    session (``USER_OPT_OUT`` / ``SUBAGENT_FRAGMENT`` / ...) and the empty
    string for an admitted one; ``reason`` keeps the human-readable detail.

    ``opening_text`` is the opening of the whole session when ``text`` is only part of
    it (a later chunk, a review excerpt): a write dispatch is recognised by how the
    conversation opens and whether anything follows, which an excerpt may no longer show.
    """
    if not cfg.enabled:
        return {
            "admit": True,
            "tier": "triage_disabled",
            "reason": "triage_disabled",
            "reason_code": "",
            "workspace": session.workspace,
        }

    meta = session.metadata if isinstance(session.metadata, dict) else {}
    has_parent = bool(meta.get("parent_session_key"))
    tp_name = Path(session.transcript_path).name if session.transcript_path else ""
    is_sub_file = is_subagent_filename(session.session_id, cfg) or (tp_name and is_subagent_filename(tp_name, cfg))
    flagged_subagent = bool(meta.get("is_subagent", False))
    subagent_record = flagged_subagent or is_sub_file or has_parent

    # 1. Explicit user ingestion (MCP ingest_session / manual UI) always bypasses auto-drop.
    #    A subagent record is not a person asking for a distillation, so for those the
    #    write-dispatch check below is answered first.
    if not subagent_record and _is_explicit_submission(session):
        return {
            "admit": True,
            "tier": "explicit",
            "reason": "explicit_user_submission",
            "reason_code": "",
            "workspace": session.workspace,
        }

    # 2. A controlled knowledge-base write dispatch describes the writing of a document,
    #    not the work behind it: distilling it again is recursion, so it never enters the queue.
    if is_controlled_write_dispatch(opening_text or text, cfg):
        return {
            "admit": False,
            "tier": "skipped",
            "reason": "controlled_write_dispatch",
            "reason_code": "SUBAGENT_FRAGMENT",
            "workspace": session.workspace,
        }

    if subagent_record and _is_explicit_submission(session):
        return {
            "admit": True,
            "tier": "explicit",
            "reason": "explicit_user_submission",
            "reason_code": "",
            "workspace": session.workspace,
        }

    # 3. Check parentage & subagent / headless fragments. A subagent record without
    #    its parent lost the context that made it meaningful: it is a fragment.
    if (is_sub_file or flagged_subagent) and not has_parent:
        return {
            "admit": False,
            "tier": "skipped",
            "reason": "subagent_or_system_fragment",
            "reason_code": map_triage_reason_code("subagent_or_system_fragment"),
            "workspace": session.workspace,
        }

    # 4. Discover workspace if empty
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

    # 6. Headless script & command checks (only for sessions without parent)
    if not has_parent:
        first_user = extract_first_user_message(text) or body_text
        first_user_clean = clean_user_message(first_user)
        if cfg.exclude_content_prefixes and (
            any(first_user_clean.startswith(prefix) for prefix in cfg.exclude_content_prefixes)
            or any(body_text.strip().startswith(prefix) for prefix in cfg.exclude_content_prefixes)
        ):
            return {
                "admit": False,
                "tier": "skipped",
                "reason": "headless_script_content_prefix_matched",
                "reason_code": "SUBAGENT_FRAGMENT",
                "workspace": workspace,
            }

        turns = count_effective_turns(body_text)
        if turns <= 2:
            lower_first = first_user_clean.lower()
            lower_body = body_text[:4000].lower()
            if any(marker in lower_first or marker in lower_body for marker in _NO_TOOL_BATCH_MARKERS):
                return {
                    "admit": False,
                    "tier": "skipped",
                    "reason": "headless_batch_no_tools_subagent",
                    "reason_code": "SUBAGENT_FRAGMENT",
                    "workspace": workspace,
                }

    # Attached subagents are admitted into queued under their parent
    if has_parent:
        return {
            "admit": True,
            "tier": "subagent_attached",
            "reason": "subagent_attached_to_parent",
            "reason_code": "",
            "workspace": workspace,
        }

    # 7. Check min_chars for regular sessions
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
