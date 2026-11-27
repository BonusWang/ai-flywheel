from __future__ import annotations

import json
import os
import re
import time
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


# ---------------------------------------------------------------- Antigravity transcripts
#
# Antigravity keeps one conversation per ``<brain>/<uuid>/`` directory and logs it to
# ``.system_generated/logs/transcript.jsonl`` inside it. Every line is one *step*: it has a
# ``step_index`` and a ``type``. Only two types are the conversation — ``USER_INPUT`` is what
# the person asked, ``PLANNER_RESPONSE`` what the model answered — and the rest is
# bookkeeping: ``GENERIC`` is raw tool output, by far the largest part of a file.

#: Step types only Antigravity writes. One of them identifies a line's origin on its own.
_ANTIGRAVITY_STEP_TYPES = frozenset({"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "CHECKPOINT"})
_ANTIGRAVITY_USER_STEP = "USER_INPUT"
_ANTIGRAVITY_ASSISTANT_STEP = "PLANNER_RESPONSE"

#: What Antigravity wraps a user step in. ``USER_REQUEST`` holds what the person typed. The
#: other two are the app's bookkeeping around it — the time, the open document, an invoked
#: skill, a settings change — and are never dialogue. An unclosed block runs to the end.
_ANTIGRAVITY_METADATA_BLOCK_RE = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>(.*?)(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE
)
_ANTIGRAVITY_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)(?:</USER_REQUEST>|\Z)", re.DOTALL | re.IGNORECASE)
_ANTIGRAVITY_REQUEST_TAG_RE = re.compile(r"</?USER_REQUEST>", re.IGNORECASE)

#: The transcript file of a conversation, as the client writes it into the uuid directory.
_ANTIGRAVITY_TRANSCRIPT_TAIL = (".system_generated", "logs", "transcript.jsonl")

#: How long an Antigravity transcript must have been left alone before a passive pass reads it.
DEFAULT_SCAN_QUIET_WINDOW_SECONDS = 900


def is_antigravity_client(client: Any) -> bool:
    """Whether a scan source or a session belongs to the Antigravity client."""
    return str(client or "").strip().lower() == "antigravity"


def is_antigravity_record(obj: Any) -> bool:
    """Whether one transcript line is a step of an Antigravity ``transcript.jsonl``.

    Decisive on its own is a step type only Antigravity writes. Any other line that carries
    a top-level ``step_index`` is an Antigravity step too — one of a type that need not be
    named here, such as ``SYSTEM_MESSAGE`` or ``ERROR_MESSAGE``.

    A Codex (``payload``) or Claude Code (``message``) envelope is never one, whatever else
    it carries: those clients keep their identity inside the envelope. A Claude Code
    ``type: "system"`` line therefore stays on the path it always took.
    """
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("payload"), dict) or isinstance(obj.get("message"), dict):
        return False
    step_type = str(obj.get("type") or "").strip().upper()
    return step_type in _ANTIGRAVITY_STEP_TYPES or "step_index" in obj


def strip_antigravity_wrappers(content: Any) -> str:
    """What a person actually typed in an Antigravity user step.

    The request inside ``<USER_REQUEST>`` when there is one; otherwise whatever the step
    says once its bookkeeping blocks and tags are gone (the approval of a document arrives
    that way, with an empty request); ``""`` when nothing is left.
    """
    text = flatten_content(content)
    if not text:
        return ""
    text = _ANTIGRAVITY_METADATA_BLOCK_RE.sub("", text)
    request = _ANTIGRAVITY_REQUEST_RE.search(text)
    if request is not None and request.group(1).strip():
        return request.group(1).strip()
    return _ANTIGRAVITY_REQUEST_TAG_RE.sub("", text).strip()


def antigravity_dialogue_text(obj: dict[str, Any]) -> str:
    """The dialogue one Antigravity step carries: ``[user] …``, ``[assistant] …`` or ``""``.

    A ``USER_INPUT`` step is its request with the wrappers removed. A ``PLANNER_RESPONSE``
    step is its ``content`` only: the model's ``thinking`` and ``tool_calls`` are not an
    answer, and most steps have no content at all. Every other step type — tool output,
    checkpoints, system and error notices — is not dialogue and yields ``""``.
    """
    step_type = str(obj.get("type") or "").strip().upper()
    if step_type == _ANTIGRAVITY_USER_STEP:
        text = strip_antigravity_wrappers(obj.get("content"))
        return f"[user] {text}" if text else ""
    if step_type == _ANTIGRAVITY_ASSISTANT_STEP:
        text = flatten_content(obj.get("content"))
        return f"[assistant] {text}" if text else ""
    return ""


def antigravity_session_id(path: Path, client: str = "") -> str:
    """The conversation id an Antigravity transcript is stored under, or ``""``.

    Every transcript is named ``transcript.jsonl``, so the file name says nothing: the uuid
    directory it sits in is the id. The layout settles that without knowing the client, so
    every caller normalises alike. A transcript moved out of the layout is still normalised
    when the caller names the client.
    """
    parts = path.parts
    if not is_antigravity_client(client):
        if tuple(part.lower() for part in parts[-3:]) != _ANTIGRAVITY_TRANSCRIPT_TAIL or len(parts) < 4:
            return ""
        return parts[-4] if _UUID_RE.fullmatch(parts[-4]) else ""
    for parent in path.parents:
        if _UUID_RE.fullmatch(parent.name):
            return parent.name
    return ""


#: A machine-written line of a user step that names the open document or the workspace. The
#: label may carry Markdown emphasis, around it or inside it: ``**Active Document:** value``.
_ANTIGRAVITY_WORKSPACE_LABEL_RE = re.compile(
    r"^\s*[\*_`]*(?:active document|workspace(?:[ _]+root)?|working directory|current directory|project root|cwd)"
    r"[\s\*_`]*[:=][ \t]*(.+?)\s*[\*_`]*$",
    re.IGNORECASE,
)
_ANTIGRAVITY_LIST_MARKER_RE = re.compile(r"^[\s>*+-]+")

#: The user step as its JSON line spells its type, whatever the spacing: a cheap test that
#: lets a long transcript be read for its user steps without parsing the other lines.
_ANTIGRAVITY_USER_STEP_QUOTED = f'"{_ANTIGRAVITY_USER_STEP}"'

#: A local path written in running text: a drive path, or an absolute POSIX path with at least
#: two segments. A URL never matches: its first slash follows a colon, the rest a slash.
_BARE_LOCAL_PATH_RE = (
    r"(?<![A-Za-z0-9_/])(?:[A-Za-z]:[\\/][^\s\"'<>|*`]+|/(?:[\w.\-@+]+[\\/])+[\w.\-@+]+)"
)
#: Every place running text may name a path, in the order it names them. A token set off in
#: backticks or double quotes is read whole when it starts like a path: spaces inside belong
#: to it, which the bare pattern cannot tell from the end of the path.
_PATH_MENTION_RE = re.compile(
    r"`(?P<backtick>[^`\n]+)`|\"(?P<dquote>[^\"\n]+)\"|(?P<bare>" + _BARE_LOCAL_PATH_RE + r")"
)
_LOCAL_PATH_RE = re.compile(_BARE_LOCAL_PATH_RE)
_LOCAL_PATH_START_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/)")
#: Punctuation a path picks up from the sentence around it.
_PATH_TRIM_CHARS = "\"'`" + ".,;:!?，。；：、）)》"

#: The words that follow a path written without quotes: up to twelve are tried as part of it.
_ANTIGRAVITY_FOLLOWING_WORDS_RE = re.compile(r"[ \t]+(\S+(?:[ \t]+\S+){0,11})")

#: The folders directly under a home directory that hold the AI tools' own files. A path in
#: them is a skill or a setting, never the workspace of a conversation.
_TOOL_CONFIG_HOMES = (".gemini", ".antigravity", ".codex", ".claude", ".agents", ".cursor", ".windsurf")

#: How far up from a path the repository root may be looked for.
_WORKSPACE_ROOT_MAX_LEVELS = 24


def _existing_path(raw_path: str) -> Path | None:
    """The absolute directory or file a written path names, when it is on this machine."""
    try:
        candidate = Path(os.path.expanduser(raw_path.strip().strip(_PATH_TRIM_CHARS)))
        if candidate.is_absolute() and (candidate.is_dir() or candidate.is_file()):
            return candidate
    except (OSError, ValueError):
        return None
    return None


def _workspace_of_directory(directory: Path, home: Path) -> str:
    """The workspace a real directory stands for, or ``""`` when it stands for none.

    The workspace is the repository root the directory sits in, or else the directory. A
    filesystem root, the home directory itself, or one of the AI tools' own folders under it
    (a skill, a setting, the transcript store) is never a workspace.
    """
    try:
        for ancestor in (directory, *directory.parents[: _WORKSPACE_ROOT_MAX_LEVELS - 1]):
            if (ancestor / ".git").exists():
                directory = ancestor
                break
        if directory.parent == directory or directory == home or any(
            directory == home / name or home / name in directory.parents for name in _TOOL_CONFIG_HOMES
        ):
            return ""
    except OSError:
        return ""
    return str(directory)


def _workspace_of_local_path(raw_path: str, home: Path, *, cut_by_blank: bool = False) -> str:
    """The workspace a local path stands for, or ``""`` when it stands for none.

    The path has to be absolute and to be on this machine: a workspace that is only guessed
    is worse than none, because the triage rules trust it. What follows its last separator
    need not exist — a word written straight after the path, or a file not created yet, still
    names a folder that does — but only when nothing cut the path short (``cut_by_blank``):
    the folder above a path that was cut is no more than a guess.
    """
    existing = _existing_path(raw_path)
    if existing is not None:
        directory = existing if existing.is_dir() else existing.parent
    else:
        if cut_by_blank:
            return ""
        try:
            candidate = Path(os.path.expanduser(raw_path.strip().strip(_PATH_TRIM_CHARS)))
            directory = candidate.parent
            if not candidate.is_absolute() or not directory.is_dir():
                return ""
        except (OSError, ValueError):
            return ""
    return _workspace_of_directory(directory, home)


def _path_with_following_words(text: str, start: int, end: int) -> tuple[str, int]:
    """A path written without quotes keeps the words that continue it on disk.

    ``…/work area/release notes.md`` written bare is matched only up to ``…/work``. The
    words after it are appended one at a time, and the longest form that is on the machine
    is kept: a path is extended only into something that exists, so a path that is complete
    is never extended by the sentence around it. Returns the path and where it ends in
    ``text``.
    """
    candidate = text[start:end]
    following = _ANTIGRAVITY_FOLLOWING_WORDS_RE.match(text, end)
    if following is None:
        return candidate, end
    used_end = end
    for word in re.finditer(r"\S+", following.group(1)):
        longer_end = following.start(1) + word.end()
        if _existing_path(text[start:longer_end]) is not None:
            candidate, used_end = text[start:longer_end], longer_end
    return candidate, used_end


def _typed_path_mentions(text: str) -> list[tuple[str, bool]]:
    """Every local path running text names, in order, each with whether a blank cut it short.

    A token set off in backticks or double quotes is read whole when it starts like a path:
    spaces inside belong to it. A bare path has no such boundary, so it ends at the first
    blank unless the words after it continue it on disk.
    """
    mentions: list[tuple[str, bool]] = []
    for mention in _PATH_MENTION_RE.finditer(text):
        quoted = mention.group("backtick") or mention.group("dquote")
        if quoted is not None and _LOCAL_PATH_START_RE.match(quoted):
            mentions.append((quoted, False))
            continue
        source = text if quoted is None else quoted
        for inner in _LOCAL_PATH_RE.finditer(source):
            candidate, end = _path_with_following_words(source, inner.start(), inner.end())
            mentions.append((candidate, end < len(source) and source[end].isspace()))
    return mentions


def antigravity_workspace(content: Any, home: Path | None = None) -> str:
    """The local workspace an Antigravity user step names, or ``""`` when it names none.

    An Antigravity transcript carries no ``cwd``, so the workspace is read, best effort and
    conservatively:

    * a machine-written line that names the open document or the workspace
      (``Active Document: …``) — a browser tab is not a workspace, and is passed over;
    * failing that, the first local path the person wrote in the request.

    A path counts only when it is on this machine. Nothing found leaves the workspace
    empty: the triage rules then treat the session as one whose workspace is unknown,
    which is the honest answer.
    """
    raw = flatten_content(content)
    if not raw:
        return ""
    home_dir = Path.home() if home is None else home
    candidates: list[tuple[str, bool]] = []
    for block in _ANTIGRAVITY_METADATA_BLOCK_RE.finditer(raw):
        for line in block.group(2).splitlines():
            labelled = _ANTIGRAVITY_WORKSPACE_LABEL_RE.match(_ANTIGRAVITY_LIST_MARKER_RE.sub("", line))
            if labelled is None:
                continue
            value = labelled.group(1)
            # "… tab 1 (/path/to/file.py) (LANGUAGE_X)" names the file inside the brackets.
            candidates.extend((inside, False) for inside in re.findall(r"\(([^()]*)\)", value))
            candidates.append((re.sub(r"\([^()]*\)", "", value), False))
    candidates.extend(_typed_path_mentions(strip_antigravity_wrappers(content)))

    for candidate, cut_by_blank in candidates:
        if not candidate.strip():
            continue
        workspace = _workspace_of_local_path(candidate, home_dir, cut_by_blank=cut_by_blank)
        if workspace:
            return workspace
    return ""


def scan_quiet_window_seconds(cfg: Any) -> int:
    """How long an Antigravity transcript must be left alone before a passive pass reads it.

    ``sessions.scan_quiet_window_seconds``; ``0`` or ``None`` switches the window off. It is
    read with a default rather than off the config model, so the two passive gates — the
    directory scan and the queue — behave the same whichever version of the configuration
    they run with.
    """
    sessions = getattr(cfg, "sessions", None)
    seconds = getattr(sessions, "scan_quiet_window_seconds", DEFAULT_SCAN_QUIET_WINDOW_SECONDS) or 0
    return max(0, int(seconds))


def transcript_is_active(mtime: float, *, quiet_seconds: int, now_ts: float | None = None) -> bool:
    """Whether a transcript last written at ``mtime`` is still inside the quiet window.

    The window is half-open: a file whose last write is exactly ``quiet_seconds`` old has
    gone quiet and is read. A window of ``0`` never holds anything back.
    """
    if quiet_seconds <= 0:
        return False
    current = time.time() if now_ts is None else float(now_ts)
    return (current - float(mtime)) < quiet_seconds


def _read_antigravity_step(obj: dict[str, Any], result: dict[str, Any]) -> bool:
    """Read one Antigravity user step into ``result``: whether the file has now said all it has to.

    A user step that says nothing — an upload, a bare slash command, a document approval
    with an empty request — is passed over for the first one that does. The workspace is
    looked for in every user step until one names it: the document the person is working on
    may be opened turns into the conversation.
    """
    if str(obj.get("type") or "").strip().upper() != _ANTIGRAVITY_USER_STEP:
        return False
    content = obj.get("content")
    if not result["workspace"]:
        result["workspace"] = antigravity_workspace(content)
    if not result["first_user_message"]:
        cleaned = clean_user_message(strip_antigravity_wrappers(content))
        if cleaned and not _is_boilerplate_user_text(cleaned):
            result["first_user_message"] = cleaned
    return bool(result["first_user_message"] and result["workspace"])


def extract_jsonl_metadata(path: Path, max_lines: int = 50, *, client: str = "") -> dict[str, Any]:
    """Extract canonical identity, parentage, subagent metadata, workspace, and title info from a JSONL file.

    ``client`` is the client the file was found for. It matters only to a client whose
    transcript carries no identity of its own (Antigravity), and then only when the file
    is no longer in the layout that names the conversation.

    ``max_lines`` bounds the opening lines that are read in full. An Antigravity transcript
    is read on past them for its user steps alone — without parsing the other lines — while
    the title or the workspace is still missing.
    """
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
    antigravity_scan_open = False

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for idx, raw in enumerate(f):
                if idx >= max_lines:
                    if not antigravity_scan_open:
                        break
                    if _ANTIGRAVITY_USER_STEP_QUOTED not in raw:
                        continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                # 0. An Antigravity step. The conversation id is the directory the transcript
                #    lives in (settled below); the title and the workspace come from its user
                #    steps, and once both are read nothing after them says more.
                if is_antigravity_record(obj):
                    antigravity_scan_open = True
                    if _read_antigravity_step(obj, result):
                        break
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
        result["canonical_session_id"] = (
            antigravity_session_id(path, client=client) or filename_uuid or path.stem
        )

    return result


def extract_jsonl_workspace(path: Path, max_lines: int = 25) -> str:
    """Inspect the opening lines of a Codex/Claude Code/Antigravity JSONL transcript to discover its workspace cwd."""
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
