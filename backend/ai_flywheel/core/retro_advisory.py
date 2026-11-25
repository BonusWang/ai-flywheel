"""Deterministic, profile-aware grouping and concise advisory for live retro clues.

Normalizes dynamic attempt counts so repeated retry-loop alerts for the same tool
collapse into a single group, and derives a concise 1-line action recommendation
aligned with the user's ``user_preferences`` (including private ``override_dir``
redlines) and ``hard_gates`` configuration — at zero token cost.
"""
from __future__ import annotations

import re
from typing import Any

from ai_flywheel.models import RetroItem, SignalKind

#: Built-in read-only / inspection tools that never mutate state.
READONLY_TOOLS_LOWER: frozenset[str] = frozenset(
    {
        "read_file",
        "read",
        "glob",
        "grep",
        "ls",
        "view_image",
        "list_dir",
        "find_by_name",
        "grep_search",
        "list_resources",
        "read_resource",
    }
)

#: Text/file editing tools whose content often legitimately contains SQL/shell text
#: (e.g. ``DELETE FROM`` or ``rm -rf`` inside tests, migrations, or docs).
TEXT_EDIT_TOOLS_LOWER: frozenset[str] = frozenset(
    {
        "edit",
        "write",
        "multiedit",
        "edit_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "notebookedit",
    }
)

#: Benign tool name fragments (plan/todo tracking & read-only MCP query tools)
#: that naturally fire repeatedly in a turn and should not trip retry-loop alerts.
BENIGN_TOOL_PATTERNS: tuple[str, ...] = (
    "update_plan",
    "todo_write",
    "todowrite",
    "task_boundary",
    "list_sessions",
    "list_resources",
    "get_status",
    "__list_",
    "__get_",
    "__read_",
    "__search_",
    "__find_",
)

_RETRY_COUNT_ZH_RE = re.compile(r"已连续尝试\s*(\d+)\s*次")
_RETRY_COUNT_EN_RE = re.compile(r"\battempt[s]?\s*[:=#]?\s*(\d+)\b", re.IGNORECASE)
_HIGH_RISK_PATTERN_RE = re.compile(r"高危命令模式\s*[`'\"]?([^`'\"（(]+)[`'\"]?")


def is_benign_tool(tool_name: str | None, extra_ignore: list[str] | None = None) -> bool:
    """Return True for read-only / status / plan tools exempt from retry-loop noise."""
    raw = (tool_name or "").strip().lower()
    if not raw:
        return False
    if raw in READONLY_TOOLS_LOWER:
        return True
    if extra_ignore:
        for entry in extra_ignore:
            token = str(entry or "").strip().lower()
            if token and (raw == token or token in raw):
                return True
    return any(pat in raw for pat in BENIGN_TOOL_PATTERNS)


def is_text_edit_tool(tool_name: str | None) -> bool:
    """Return True when the tool edits source/text files rather than executing commands."""
    raw = (tool_name or "").strip().lower()
    return bool(raw and raw in TEXT_EDIT_TOOLS_LOWER)


def normalize_retro_summary(summary: str) -> str:
    """Strip variable retry counts so attempt 3/4/5 collapse into one canonical summary."""
    text = (summary or "").strip()
    text = _RETRY_COUNT_ZH_RE.sub("已连续尝试 N 次", text)
    text = _RETRY_COUNT_EN_RE.sub("attempt N", text)
    return re.sub(r"\s+", " ", text)


def extract_retry_attempt(item: RetroItem) -> int:
    """Extract numeric attempt count from detail or summary (0 when unavailable)."""
    detail = item.detail if isinstance(item.detail, dict) else {}
    raw_attempt = detail.get("attempt")
    if isinstance(raw_attempt, int) and raw_attempt > 0:
        return raw_attempt
    if isinstance(raw_attempt, str) and raw_attempt.isdigit():
        return int(raw_attempt)
    m = _RETRY_COUNT_ZH_RE.search(item.summary or "")
    if m:
        return int(m.group(1))
    m = _RETRY_COUNT_EN_RE.search(item.summary or "")
    if m:
        return int(m.group(1))
    return 0


def _extract_high_risk_pattern(item: RetroItem) -> str:
    detail = item.detail if isinstance(item.detail, dict) else {}
    pat = str(detail.get("pattern") or "").strip()
    if pat:
        return pat
    m = _HIGH_RISK_PATTERN_RE.search(item.summary or "")
    return m.group(1).strip() if m else ""


def build_retro_group_key(item: RetroItem) -> str:
    """Canonical grouping key independent of dynamic retry attempt numbers."""
    kind = item.kind.value if isinstance(item.kind, SignalKind) else str(item.kind)
    tool = (item.tool_name or "").strip().lower()
    norm = normalize_retro_summary(item.summary)
    if kind == SignalKind.RETRY_LOOP.value and tool:
        return f"retry_loop::{tool}"
    if kind == SignalKind.HIGH_RISK_TOOL.value and tool:
        pattern = _extract_high_risk_pattern(item).lower()
        return f"high_risk_tool::{tool}::{pattern or norm}"
    return f"{kind}::{norm}"


def _match_user_redline(user_prefs: dict[str, Any], keywords: tuple[str, ...]) -> str:
    """Pick the highest-priority personal non-negotiable redline matching keywords in order."""
    non_neg = user_prefs.get("non_negotiables") or []
    if not isinstance(non_neg, list):
        return ""
    lines = [str(line or "").strip() for line in non_neg if str(line or "").strip()]
    for kw in keywords:
        for text in lines:
            if kw in text.lower():
                return text
    return ""


def advise_retro_item(
    item: RetroItem,
    user_prefs: dict[str, Any] | None = None,
    hard_gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a concise, profile-aligned action recommendation for a retro clue."""
    prefs = user_prefs if isinstance(user_prefs, dict) else {}
    gates = hard_gates if isinstance(hard_gates, dict) else {}
    breaker_cfg = gates.get("retry_breaker") if isinstance(gates.get("retry_breaker"), dict) else {}
    extra_ignore = breaker_cfg.get("ignore_tools") if isinstance(breaker_cfg.get("ignore_tools"), list) else []

    kind = item.kind.value if isinstance(item.kind, SignalKind) else str(item.kind)
    tool = (item.tool_name or "").strip()
    summary = (item.summary or "").strip()
    summary_low = summary.lower()
    attempt = extract_retry_attempt(item)
    group_key = build_retro_group_key(item)
    norm_summary = normalize_retro_summary(summary)

    # 1. Benign read-only / plan-update tool false positive on retry_loop
    if kind == SignalKind.RETRY_LOOP.value and is_benign_tool(tool, extra_ignore):
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "ignore",
            "verdict_label": "建议忽略（工具噪音）",
            "recommended_action": "dismissed",
            "reason": "属计划/只读类工具正常高频调用，非死循环，建议整组忽略。",
            "matched_preference": "",
        }

    # 2. Text-editing tool tripping high-risk SQL/shell text pattern
    if kind == SignalKind.HIGH_RISK_TOOL.value and is_text_edit_tool(tool):
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "ignore",
            "verdict_label": "建议忽略（编辑误报）",
            "recommended_action": "dismissed",
            "reason": "仅为编辑源码/文本命中关键词，未实际执行命令，建议整组忽略。",
            "matched_preference": "",
        }

    # 3. Genuine execution-tool high-risk alert
    if kind == SignalKind.HIGH_RISK_TOOL.value:
        redline = _match_user_redline(prefs, ("授权", "边界", "高危", "风险", "备份", "验证"))
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "review",
            "verdict_label": "建议核对边界",
            "recommended_action": "resolved",
            "reason": "执行工具命中高危模式，确认授权范围与备份后标已处理。",
            "matched_preference": redline,
        }

    # 4. Genuine execution-tool retry loop
    if kind == SignalKind.RETRY_LOOP.value:
        redline = _match_user_redline(prefs, ("根因", "绕过", "盲", "验证", "检查"))
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "review",
            "verdict_label": "建议查根因",
            "recommended_action": "resolved",
            "reason": "执行工具同输入反复重试，确认是否为环境/脚本坑后决定留存或忽略。",
            "matched_preference": redline,
        }

    # 5. High-value verification / evidence / recurrence signals
    evidence_markers = ("未经验证", "更正", "推翻", "实测", "断言", "证据", "权威", "根因", "复发")
    if kind in {SignalKind.MISSING_EVIDENCE.value, SignalKind.RULE_RECURRENCE.value} or any(
        kw in summary_low for kw in evidence_markers
    ):
        redline = _match_user_redline(prefs, ("权威", "臆测", "验证", "证据", "检查", "根因"))
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "keep",
            "verdict_label": "高价值 · 建议留存",
            "recommended_action": "resolved",
            "reason": "命中实证/权威源纠偏信号，建议保留进入沉淀或提炼为避坑规则。",
            "matched_preference": redline,
        }

    # 6. Blocker or general custom signal
    if kind == SignalKind.BLOCKER.value:
        redline = _match_user_redline(prefs, ("风险", "不确定", "阻塞", "根因"))
        return {
            "group_key": group_key,
            "normalized_summary": norm_summary,
            "attempt": attempt,
            "verdict": "review",
            "verdict_label": "建议核对阻塞",
            "recommended_action": "resolved",
            "reason": "执行受阻记录，若已形成解法可保留沉淀，否则直接忽略。",
            "matched_preference": redline,
        }

    return {
        "group_key": group_key,
        "normalized_summary": norm_summary,
        "attempt": attempt,
        "verdict": "review",
        "verdict_label": "建议快速确认",
        "recommended_action": "resolved",
        "reason": "核对是否包含可复用经验；无复用价值可直接忽略。",
        "matched_preference": "",
    }
