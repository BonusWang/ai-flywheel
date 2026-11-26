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



_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:[a-z0-9_-]+)\s+(?:session|archived session|·)\s+",
    re.IGNORECASE,
)
_WRAPPER_BLOCKS_RE = re.compile(
    r"<(?:system-reminder|task-notification|app-context)>.*?</(?:system-reminder|task-notification|app-context)>",
    re.DOTALL | re.IGNORECASE,
)
_SKILL_REF_RE = re.compile(r"\[\$[a-z0-9_-]+\]\([^)]+\)", re.IGNORECASE)
_USER_ROLE_PREFIX_RE = re.compile(r"^\[(?:user|human|queue-operation)\]\s*", re.IGNORECASE)
_NO_TOOL_BATCH_MARKERS = (
    "all tools disabled",
    "所有工具已关闭",
    "严禁调用任何工具",
    "所有工具均已禁用",
    "tools are disabled",
    "禁用所有工具",
)


def flatten_content(content: Any) -> str:
    """Flatten textual content from strings or block lists, stripping tool/thinking blocks."""
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


_BOILERPLATE_USER_PREFIXES = (
    "<app-context>",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<system-reminder>",
    "# agents.md instructions",
    "# claude.md",
)


def _is_boilerplate_user_text(text: str) -> bool:
    stripped = (text or "").lstrip("\ufeff").strip().lower()
    return any(stripped.startswith(p) for p in _BOILERPLATE_USER_PREFIXES)


def clean_user_message(msg: str) -> str:
    """Strip wrappers, role markers, skill references, and truncate to <=64 chars."""
    if not msg:
        return ""
    text = msg.lstrip("\ufeff").strip()
    text = _USER_ROLE_PREFIX_RE.sub("", text).strip()
    text = _WRAPPER_BLOCKS_RE.sub("", text).strip()
    text = _SKILL_REF_RE.sub("", text).strip()
    text = _USER_ROLE_PREFIX_RE.sub("", text).strip()
    single_line = " ".join(text.split())
    if len(single_line) > 64:
        single_line = single_line[:64].rstrip()
    return single_line


def extract_first_user_message(text: str) -> str:
    """Extract the first user dialogue turn from dialogue or transcript text."""
    if not text:
        return ""
    match = re.search(
        r"\[(?:user|human)\]\s*(.*?)(?=\[(?:assistant|model|user|human)\]|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    lines: list[str] = []
    for line in text.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        if re.match(r"^(?:Title|Summary):", line_s, re.IGNORECASE):
            continue
        if re.match(r"^\[(?:assistant|model)\]", line_s, re.IGNORECASE):
            break
        lines.append(line_s)
        if len(lines) >= 3:
            break
    return " ".join(lines)


def derive_readable_title(
    client: str,
    session_id: str,
    *,
    explicit_title: str = "",
    custom_title: str = "",
    text: str = "",
    workspace: str = "",
) -> str:
    """Generate a human-readable title following the 4-tier precedence:

    1. Explicit valid title (not matching the old placeholder pattern)
    2. Claude Code custom-title
    3. First substantive user message (cleaned and truncated to <=64 chars)
    4. Fallback: '[workspace_dir] client · <tail-8-id>' or 'client · <tail-8-id>'
    """
    client_clean = (client or "generic").strip().lower()

    # Tier 1: Explicit valid title
    exp = (explicit_title or "").strip()
    if exp and not _PLACEHOLDER_TITLE_RE.match(exp):
        return exp

    # Tier 2: Custom title (Claude Code)
    ct = (custom_title or "").strip()
    if ct:
        return ct

    # Tier 3: First valid user message
    user_turn = extract_first_user_message(text) or text
    cleaned_user = clean_user_message(user_turn)
    if cleaned_user and len(cleaned_user) >= 2:
        return cleaned_user

    # Tier 4: Fallback '[dir] client · <tail-8-id>'
    tail_id = session_id[-8:] if len(session_id) >= 8 else session_id
    ws = (workspace or "").strip()
    if ws:
        normalized_ws = ws.replace("\\", "/").rstrip("/")
        ws_name = Path(normalized_ws).name or normalized_ws
        if ws_name:
            return f"[{ws_name}] {client_clean} · {tail_id}"
    return f"{client_clean} · {tail_id}"


def extract_jsonl_metadata(path: Path, max_lines: int = 50) -> dict[str, Any]:
    """Extract canonical identity, parentage, subagent metadata, workspace, and title info from a JSONL file."""
    result: dict[str, Any] = {
        "canonical_session_id": "",
        "parent_session_id": "",
        "parent_session_key": "",
        "is_subagent": False,
        "subagent_nickname": "",
        "subagent_role": "",
        "workspace": "",
        "custom_title": "",
        "first_user_message": "",
    }
    if not path.exists() or not path.is_file() or path.suffix.lower() != ".jsonl":
        return result

    uuid_matches = _UUID_RE.findall(path.stem)
    filename_uuid = uuid_matches[0] if uuid_matches else ""

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

                # 1. Discover workspace if not yet set
                if not result["workspace"]:
                    for key in ("cwd", "workspace", "project_root"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            result["workspace"] = val.strip()
                            break
                    if not result["workspace"] and isinstance(obj.get("payload"), dict):
                        payload = obj["payload"]
                        for key in ("cwd", "workspace", "project_root"):
                            val = payload.get(key)
                            if isinstance(val, str) and val.strip():
                                result["workspace"] = val.strip()
                                break

                # 2. Codex session_meta (never match response_item payload.id = 'msg_...')
                if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    payload = obj["payload"]
                    p_id = str(payload.get("id") or "").strip()
                    if p_id and not result["canonical_session_id"]:
                        result["canonical_session_id"] = p_id

                    # parent_session_id
                    parent_id = str(payload.get("parent_thread_id") or "").strip()
                    source = payload.get("source")
                    if not parent_id and isinstance(source, dict):
                        sub = source.get("subagent")
                        if isinstance(sub, dict):
                            spawn = sub.get("thread_spawn")
                            if isinstance(spawn, dict):
                                parent_id = str(spawn.get("parent_thread_id") or "").strip()
                    if not parent_id:
                        sess_id = str(payload.get("session_id") or "").strip()
                        if sess_id and sess_id != p_id:
                            parent_id = sess_id
                    if parent_id:
                        result["parent_session_id"] = parent_id
                        result["is_subagent"] = True

                    # is_subagent checks
                    if payload.get("thread_source") == "subagent" or (isinstance(source, dict) and "subagent" in source):
                        result["is_subagent"] = True

                    # nickname / role
                    nickname = str(payload.get("agent_nickname") or "").strip()
                    role = str(payload.get("agent_role") or "").strip()
                    if not nickname and isinstance(source, dict):
                        sub = source.get("subagent")
                        if isinstance(sub, dict):
                            nickname = str(sub.get("agent_nickname") or "").strip()
                            spawn = sub.get("thread_spawn")
                            if not nickname and isinstance(spawn, dict):
                                nickname = str(spawn.get("agent_nickname") or "").strip()
                    if not role and isinstance(source, dict):
                        sub = source.get("subagent")
                        if isinstance(sub, dict):
                            role = str(sub.get("agent_role") or "").strip()
                            spawn = sub.get("thread_spawn")
                            if not role and isinstance(spawn, dict):
                                role = str(spawn.get("agent_role") or "").strip()
                    if nickname:
                        result["subagent_nickname"] = nickname
                    if role:
                        result["subagent_role"] = role

                # 3. Claude Code fields
                if obj.get("type") == "custom-title":
                    ct = str(obj.get("customTitle") or "").strip()
                    if ct:
                        result["custom_title"] = ct
                if not result["canonical_session_id"] and obj.get("sessionId"):
                    result["canonical_session_id"] = str(obj.get("sessionId")).strip()
                if obj.get("isSidechain") is True:
                    result["is_subagent"] = True

                # 4. First user message
                if not result["first_user_message"]:
                    user_text = ""
                    role = str(obj.get("role") or obj.get("type") or "").strip().lower()
                    if role in {"user", "human"}:
                        user_text = flatten_content(obj.get("content") or obj.get("text") or obj.get("message"))
                    elif isinstance(obj.get("message"), dict):
                        msg = obj["message"]
                        mrole = str(msg.get("role") or "").strip().lower()
                        if mrole in {"user", "human"}:
                            user_text = flatten_content(msg.get("content"))
                    elif isinstance(obj.get("payload"), dict):
                        payload = obj["payload"]
                        ptype = str(payload.get("type") or "").strip().lower()
                        prole = str(payload.get("role") or "").strip().lower()
                        if ptype == "user_message" or prole in {"user", "human"}:
                            user_text = flatten_content(payload.get("content") or payload.get("message") or payload.get("text"))
                    if user_text and not _is_boilerplate_user_text(user_text):
                        cleaned = clean_user_message(user_text)
                        if cleaned and not _is_boilerplate_user_text(cleaned):
                            result["first_user_message"] = cleaned

    except OSError:
        pass

    if not result["canonical_session_id"]:
        result["canonical_session_id"] = filename_uuid or path.stem

    return result


def extract_jsonl_workspace(path: Path, max_lines: int = 25) -> str:
    """Inspect the opening lines of a Codex/Claude Code JSONL transcript to discover its workspace cwd."""
    meta = extract_jsonl_metadata(path, max_lines=max_lines)
    return meta.get("workspace") or ""


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
    if meta.get("discovered_by") in {"p00_reconcile_scan", "on_archive", "archive"}:
        return False
    # Direct MCP ingest_session / manual registration pushes inline content (not just a transcript path)
    if session.content and session.content.strip():
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

    # 2. Check parentage & subagent / headless fragments
    meta = session.metadata if isinstance(session.metadata, dict) else {}
    has_parent = bool(meta.get("parent_session_key"))
    tp_name = Path(session.transcript_path).name if session.transcript_path else ""
    is_sub_file = is_subagent_filename(session.session_id, cfg) or (tp_name and is_subagent_filename(tp_name, cfg))

    if is_sub_file and not has_parent:
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
