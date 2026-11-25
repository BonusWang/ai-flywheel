"""The wire contract of the runner layer.

Everything the runners agree on lives here: where a write plan or a session review came
from, why a reply was refused, the diagnostics the orchestrator traces, and the
deterministic text helpers every planner shares.

The runners themselves live in `heuristic.py` (dry-run) and `clients.py` (CLI and API);
the prompts they send live in `prompts.py`; the item-level normalization lives in
`normalizers.py` and the reply-to-outcome conversion in `outcomes.py`.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ai_flywheel.models import (
    CreateRuleRequest,
    PlannedWrite,
    RetroItem,
    RuleRecord,
    SessionRecord,
)

# ---- where a plan came from ----

PLAN_SOURCE_MODEL = "model"
PLAN_SOURCE_CLI = "cli"
PLAN_SOURCE_HEURISTIC = "heuristic"

SUPPORTED_TARGET_KINDS = ("knowledge_doc", "flywheel_rule", "skill_improvement")

# Every P03 item must carry these keys, whatever its kind.
P03_ITEM_REQUIRED_KEYS = ("target_kind", "action", "title", "summary", "relative_path", "source_session_keys")

# Expected top-level structure shown to the model when its reply is malformed JSON. With a
# contract-governed Wiki every knowledge_doc also declares its route (see `prompts.py`).
P03_REPAIR_ITEM = (
    '{"target_kind":"knowledge_doc|flywheel_rule|skill_improvement","action":"create|update|register_rule",'
    '"title":"简洁中文标题","summary":"一句话说明这项写入沉淀了什么","relative_path":"<path or null>",'
    '"source_session_keys":["<session_key>"],"content_preview":"..."'
)
P03_REPAIR_SCHEMA_HINT = '{"planned_writes":[' + P03_REPAIR_ITEM + '}]}'
P03_REPAIR_SCHEMA_HINT_WITH_ROUTE = (
    '{"planned_writes":['
    + P03_REPAIR_ITEM
    + ',"wiki_route":{"page_type":"...","project":"... or null","reason":"..."}}]}'
)

_NO_FALLBACK_HINT = "需要本地启发式计划时把 runner.mode 切到 dry_run。"

_NO_REVIEW_FALLBACK_HINT = "需要本地启发式审查时把 runner.mode 切到 dry_run。"

# ---- where a review came from ----

# The review has the same three sources as the write plan.
REVIEW_SOURCE_MODEL = PLAN_SOURCE_MODEL
REVIEW_SOURCE_CLI = PLAN_SOURCE_CLI
REVIEW_SOURCE_HEURISTIC = PLAN_SOURCE_HEURISTIC
# Written by the orchestrator when a runner's review does not say where it came from.
REVIEW_SOURCE_UNDECLARED = "undeclared"

# The P02 prompt carries the first sessions only. The same bound decides whether an empty review is usable.
P02_MAX_SESSIONS = 6

# Why a model / CLI review could not be used. Codes only: the reply itself never enters diagnostics.
REVIEW_EMPTY_REPLY = "EMPTY_REPLY"
REVIEW_UNPARSABLE_REPLY = "UNPARSABLE_REPLY"
REVIEW_FINDINGS_NOT_LIST = "FINDINGS_NOT_LIST"
REVIEW_EMPTY_FINDINGS = "EMPTY_FINDINGS"

_RAW_ID_TITLE_RE = re.compile(
    r"^(?:codex|claude|claude-code|antigravity|cursor|session|chat)\s*[·:：\-]\s*[0-9a-f\-]{6,}$",
    re.IGNORECASE,
)


@dataclass
class PlanDiagnostics:
    """Where a write plan came from and what happened to every item the planner returned.

    Item records carry the index, kind, action and reason codes only; draft bodies never enter diagnostics.
    """

    source: str
    returned_count: int = 0
    kept_count: int = 0
    over_limit_count: int = 0
    completed_fields: dict[str, int] = field(default_factory=dict)
    dropped_items: list[dict[str, Any]] = field(default_factory=list)
    missing_source_session_keys: int = 0
    relabeled_skill_actions: list[str] = field(default_factory=list)
    deferred_skill_candidates: list[dict[str, Any]] = field(default_factory=list)
    cache_file: str | None = None
    cache_hit: bool = False
    # What the reply was: its length, whether a format repair ran, why it was not JSON, and
    # what the model call itself cost (tokens, duration, attempts). Bodies never enter diagnostics.
    output_chars: int | None = None
    repair_attempted: bool = False
    parse_error: str | None = None
    repair_parse_error: str | None = None
    repair_error: str | None = None
    call_telemetry: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_source": self.source,
            "returned_count": self.returned_count,
            "kept_count": self.kept_count,
            "over_limit_count": self.over_limit_count,
            "completed_fields": dict(self.completed_fields),
            "dropped_items": [dict(item) for item in self.dropped_items],
            "missing_source_session_keys": self.missing_source_session_keys,
            "relabeled_skill_actions": list(self.relabeled_skill_actions),
            "deferred_skill_candidates": [dict(item) for item in self.deferred_skill_candidates],
            "cache_file": self.cache_file,
            "cache_hit": self.cache_hit,
            "output_chars": self.output_chars,
            "repair_attempted": self.repair_attempted,
            "parse_error": self.parse_error,
            "repair_parse_error": self.repair_parse_error,
            "repair_error": self.repair_error,
            "call_telemetry": dict(self.call_telemetry) if self.call_telemetry else None,
        }


@dataclass
class PlanOutcome:
    """A write plan together with the diagnostics the orchestrator needs to trace it."""

    planned_writes: list[PlannedWrite]
    diagnostics: PlanDiagnostics


class PlanUnavailableError(RuntimeError):
    """An api/cli runner could not turn the model output into a usable write plan.

    Raised instead of silently substituting the heuristic planner; carries the per-item reasons.
    """

    def __init__(self, message: str, diagnostics: PlanDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class ModelCallError(RuntimeError):
    """A model call that did not complete: a transport timeout or a connection failure.

    Carries a sanitized summary (stage, threshold, attempts, exception type). The gateway is
    never named: an HTTP error keeps its status code, a transport error keeps its type.
    """

    def __init__(
        self,
        message: str,
        *,
        summary: dict[str, Any],
        error_type: str,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.summary = summary
        self.error_type = error_type
        self.telemetry = dict(telemetry) if telemetry else None


@dataclass
class ReviewDiagnostics:
    """Where a session review came from and, when it is unusable, why.

    Counts and reason codes only; the reply itself never enters diagnostics.
    """

    source: str
    sessions_sent: int = 0
    finding_count: int = 0
    reason_code: str | None = None
    output_chars: int | None = None
    repair_attempted: bool = False
    parse_error: str | None = None
    repair_parse_error: str | None = None
    # Exception type raised by the format repair call when that call did not complete.
    repair_error: str | None = None
    # What the model call itself cost: attempts, duration, served model and token usage.
    call_telemetry: dict[str, Any] | None = None
    cache_file: str | None = None
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sessions_sent": self.sessions_sent,
            "finding_count": self.finding_count,
            "reason_code": self.reason_code,
            "output_chars": self.output_chars,
            "repair_attempted": self.repair_attempted,
            "parse_error": self.parse_error,
            "repair_parse_error": self.repair_parse_error,
            "repair_error": self.repair_error,
            "call_telemetry": dict(self.call_telemetry) if self.call_telemetry else None,
            "cache_file": self.cache_file,
            "cache_hit": self.cache_hit,
        }


class ReviewUnavailableError(RuntimeError):
    """An api/cli runner could not turn the model or CLI reply into a usable session review.

    Raised instead of silently substituting the heuristic reviewer; carries the reason.
    """

    def __init__(self, message: str, diagnostics: ReviewDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


# ---------------- text and slug helpers ----------------


def _clean_title(raw_title: str, fallback_text: str = "", fallback_label: str = "协作经验沉淀") -> str:
    """Replace raw client+UUID titles (e.g. 'codex · 01a2072f-9a5d') with a readable concise Chinese title."""
    title = (raw_title or "").strip()
    if title and not _RAW_ID_TITLE_RE.match(title):
        return title[:48]
    candidate = re.sub(r"[\r\n#>*`]+", " ", (fallback_text or "")).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    if candidate:
        # Take first meaningful sentence/clause
        clause = re.split(r"[。！？!?；;\n]", candidate)[0].strip()
        if len(clause) >= 4:
            return clause[:36]
    return fallback_label


_PRIVATE_TAG_STRIP_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def _slugify(text: str) -> str:
    stripped = _PRIVATE_TAG_STRIP_RE.sub("", text or "")
    cleaned = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", stripped).strip("-").lower()
    return cleaned[:42] or "session-lesson"


def _ascii_slug(text: str, fallback: str = "custom-workflow") -> str:
    stripped = _PRIVATE_TAG_STRIP_RE.sub("", text or "")
    cleaned = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    if len(cleaned) < 2:
        return fallback
    return cleaned[:48]


def _coerce_str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str) and val.strip():
        return [s.strip() for s in re.split(r"[\n;；]+", val) if s.strip()]
    return []


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_int(value: Any) -> int | None:
    """A token count from a model reply, or None when the reply does not carry one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _session_keys(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n,;，；]+", value) if part.strip()]
    if isinstance(value, list):
        return [part.strip() for part in value if isinstance(part, str) and part.strip()]
    return []


def normalize_rule_payload(item: PlannedWrite) -> dict[str, Any]:
    """Normalize LLM or heuristic rule_payload so CreateRuleRequest validation never crashes in P04."""
    raw = dict(item.rule_payload) if isinstance(item.rule_payload, dict) else {}

    title = str(raw.get("title") or raw.get("name") or item.title or "").strip()
    if not title:
        title = "协作避坑规则"

    domain = str(raw.get("domain") or raw.get("category") or "engineering").strip() or "engineering"

    projects = _coerce_str_list(raw.get("projects"))
    if not projects:
        projects = ["generic"]

    triggers = _coerce_str_list(raw.get("triggers") or raw.get("keywords"))
    if not triggers:
        # Derive simple triggers from title tokens
        tokens = re.findall(r"[a-zA-Z0-9_-]{3,}|[一-鿿]{2,6}", title)
        triggers = list(dict.fromkeys(tokens))[:4] or ["review"]

    checklist = _coerce_str_list(raw.get("checklist") or raw.get("steps") or raw.get("checks"))
    for alt_key in ("rule", "requirement", "action", "must_do", "description", "scope"):
        extra = raw.get(alt_key)
        if isinstance(extra, str) and extra.strip():
            prefix = "适用范围：" if alt_key == "scope" else ""
            entry = f"{prefix}{extra.strip()}"
            if entry not in checklist:
                checklist.append(entry)
    if not checklist and item.summary:
        checklist.append(item.summary.strip())

    forbidden = _coerce_str_list(
        raw.get("forbidden_routes") or raw.get("forbidden") or raw.get("avoid") or raw.get("donts")
    )
    source_pointers = _coerce_str_list(raw.get("source_pointers"))
    if item.relative_path and item.relative_path not in source_pointers:
        source_pointers.append(item.relative_path)

    origin_session = raw.get("origin_session_key")
    if not origin_session and item.source_session_keys:
        origin_session = item.source_session_keys[0]

    normalized = {
        "rule_id": raw.get("rule_id") or None,
        "title": title,
        "domain": domain,
        "projects": projects,
        "triggers": triggers[:8],
        "checklist": checklist[:10] or ["执行前复核相关先例并做真实验证"],
        "forbidden_routes": forbidden[:8],
        "applicable_skills": _coerce_str_list(raw.get("applicable_skills")),
        "source_pointers": source_pointers,
        "origin_session_key": str(origin_session) if origin_session else None,
    }
    # Validate early via Pydantic model so downstream P04 is guaranteed clean
    validated = CreateRuleRequest.model_validate(normalized)
    out = validated.model_dump(mode="json")
    # Preserve optional observation linkage metadata for P04 promotion bookkeeping
    if raw.get("root_cause_key"):
        out["root_cause_key"] = str(raw["root_cause_key"])
    if raw.get("obs_id"):
        out["obs_id"] = str(raw["obs_id"])
    return out


# ---------------- reply parsing ----------------


def _extract_json_block(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _json_parse_error(text: str) -> str:
    """Why a reply holds no JSON object: a code, or the decoder's words about the reply's outermost braces.

    The decoder's message names the error and its position, never a fragment of the reply.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return "NO_BRACES"
    try:
        # Nothing closes the first brace: the reply is cut short, so decode what there is and say where it broke off.
        json.loads(text[start : end + 1] if end > start else text[start:])
    except (json.JSONDecodeError, RecursionError) as exc:
        return str(exc) if end > start else f"UNTERMINATED_OBJECT ({exc})"
    return "NO_TOP_LEVEL_OBJECT"


# What the parse codes mean, for the failure message. A decoder message needs no translation.
_JSON_ERROR_NOTES = {
    "NO_BRACES": "回复里没有 JSON 对象。",
    "UNTERMINATED_OBJECT": "JSON 对象没有闭合，回复像是被截断。",
    "NO_TOP_LEVEL_OBJECT": "最外层花括号里不是 JSON 对象。",
}


def _json_error_note(parse_error: str) -> str:
    for code, note in _JSON_ERROR_NOTES.items():
        if parse_error == code or parse_error.startswith(f"{code} ("):
            return note
    return f"JSON 解码报错：{parse_error}。"


# ---------------- the runner interface ----------------


class BaseWorkflowRunner(ABC):
    """Abstract AI execution runner (supports OpenAI-compatible API, local CLI subprocess, and dry-run)."""

    @abstractmethod
    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        """P02: Extract facts, incidents, candidate rules, and repeated workflow candidates for Skill routing."""

    @abstractmethod
    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> list[PlannedWrite]:
        """P03: Deduplicate against existing rules & skills and generate controlled write plan."""

    def plan_writes_detailed(self, *args: Any, **kwargs: Any) -> PlanOutcome:
        """P03 plan plus the diagnostics the orchestrator traces (source, drops, deferred candidates)."""
        planned = self.plan_writes(*args, **kwargs)
        return PlanOutcome(
            planned,
            PlanDiagnostics(source=PLAN_SOURCE_HEURISTIC, returned_count=len(planned), kept_count=len(planned)),
        )

