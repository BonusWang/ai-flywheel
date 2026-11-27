"""Deterministic normalization of one planned item.

The planner (model, CLI or heuristic) proposes an item; this module decides what that
item really is. It fills a missing title or summary from fixed sources, refuses a
draft that carries a credential, relabels a Skill action that contradicts the state of
its target directory, and drops an item that cannot be written at all.

Nothing here calls a model: the same input always yields the same write.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ai_flywheel.adapters.skills import (
    SkillWriterAdapter,
    inspect_skill_at,
    normalize_update_addition,
)
from ai_flywheel.adapters.runners.contracts import (
    SUPPORTED_TARGET_KINDS,
    _ascii_slug,
    _as_text,
    _clean_title,
    _session_keys,
    _slugify,
    normalize_rule_payload,
)
from ai_flywheel.core.privacy import SecretLeakError, mask_credential_shapes, sanitize_and_check_secrets
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import SENSITIVE_CONFIG_PAGE_TYPE, PlannedWrite


class _DropItem(Exception):
    """Internal signal: a planned-write item is not usable. Args are reason codes."""

    def __init__(self, *reasons: str) -> None:
        super().__init__(*reasons)
        self.reasons = list(reasons)


_SENTENCE_END_RE = re.compile(r"[。！？!?；;\n]")


def _append_reason(base: str, suffix: str) -> str:
    left = (base or "").strip().rstrip("。；; ")
    right = (suffix or "").strip().lstrip("。；; ")
    if not left:
        return right
    if not right:
        return left
    return f"{left}；{right}"


def _draft_frontmatter(content: str) -> dict[str, Any]:
    """Top-level YAML mapping of a markdown draft, or an empty dict when there is none."""
    text = (content or "").strip()
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _draft_body(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return text


def _h1(body: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*#*\s*$", body or "", re.MULTILINE)
    return re.sub(r"[*_`]+", "", match.group(1)).strip()[:48] if match else ""


def _first_sentence(body: str) -> str:
    """First complete sentence of a markdown body, without frontmatter, headings, lists or emphasis marks."""
    fenced = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith(("#", "|", "![")) or set(line) <= set("-*_= "):
            continue
        text = re.sub(r"^(?:[-*+]|\d+[.、)])\s+", "", line.lstrip("> ").strip())
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[*_`~]+", "", text).strip()
        sentence = _SENTENCE_END_RE.split(text, maxsplit=1)[0].strip()
        if len(sentence) >= 8:
            return sentence[:120]
    return ""


def _first_list_item(value: Any) -> str:
    if isinstance(value, list):
        return next((_as_text(entry) for entry in value if _as_text(entry)), "")
    return _as_text(value)


def _first_checklist_item(rule_payload: dict[str, Any]) -> str:
    for key in ("checklist", "steps", "checks", "requirements", "requirement", "rule", "must_do"):
        text = _first_list_item(rule_payload.get(key))
        if text:
            return text
    return ""


def _complete_missing_fields(raw_item: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fill a missing `title` / `summary` from fixed sources so a model item is kept, not dropped.

    Returns the repaired item and the names of the fields that were completed.
    """
    item = dict(raw_item)
    for key in ("content_preview", "rule_payload", "skill_route", "approval_reason", "requires_approval", "approved"):
        if key in item and item[key] is None:
            del item[key]
    if "source_session_keys" in item:
        item["source_session_keys"] = _session_keys(item["source_session_keys"])

    kind = item["target_kind"].strip().lower() if isinstance(item.get("target_kind"), str) else ""
    content = item.get("content_preview") if isinstance(item.get("content_preview"), str) else ""
    frontmatter = _draft_frontmatter(content)
    body = _draft_body(content)
    payload = item.get("rule_payload") if isinstance(item.get("rule_payload"), dict) else {}
    route = item.get("skill_route") if isinstance(item.get("skill_route"), dict) else {}
    completed: list[str] = []

    title = _as_text(item.get("title"))
    if not title:
        if kind == "flywheel_rule":
            candidates = (payload.get("title"), payload.get("name"), _first_checklist_item(payload), _first_sentence(body))
        elif kind == "skill_improvement":
            candidates = (_h1(body), route.get("target_skill"), frontmatter.get("name"), _first_sentence(body))
        else:
            candidates = (frontmatter.get("title"), _h1(body), _first_sentence(body))
        title = next((_as_text(value) for value in candidates if _as_text(value)), "")
        if title:
            item["title"] = title
            completed.append("title")

    summary = _as_text(item.get("summary"))
    if not summary:
        if kind == "flywheel_rule":
            candidates = (_first_checklist_item(payload), title)
        elif kind == "skill_improvement":
            candidates = (frontmatter.get("description"), route.get("reason"), item.get("approval_reason"), _first_sentence(body), title)
        else:
            candidates = (_first_sentence(body), title)
        summary = next((_as_text(value) for value in candidates if _as_text(value)), "")
        if summary:
            item["summary"] = summary
            completed.append("summary")
    return item, completed


def _substantive_lines(text: str) -> list[str]:
    """The lines of a Markdown block that carry meaning: no blanks, no bare markers."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        stripped = re.sub(r"^#{1,6}\s*", "", line)
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.、)]\s+", "", stripped)
        if not stripped.strip():
            # A bare heading marker or a bare bullet carries no content of its own.
            continue
        lines.append(line)
    return lines


def _addition_adds_nothing(addition: str, existing_text: str) -> bool:
    """True when every meaningful line of the appended section is already in the file.

    An update that repeats what the file already says changes nothing, so it is dropped
    instead of being written as a duplicate section.
    """
    substantive = _substantive_lines(addition)
    if not substantive:
        return True
    existing_lines = {line.strip() for line in (existing_text or "").splitlines()}
    existing_headings: set[str] = set()
    for line in existing_lines:
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            existing_headings.add(match.group(1).strip())
    for line in substantive:
        if line in existing_lines:
            continue
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            # A dated heading ("…（2026-11-25 补充）") is the same section when the base title exists.
            base = re.sub(r"（\d{4}-\d{2}-\d{2}\s*补充）\s*$", "", match.group(1).strip()).strip()
            if base in existing_headings:
                continue
        return False
    return True


# ---------------- P02: the domain signals of one finding ----------------

# The four domains a finding's optional `domain_signals` may record.
DOMAIN_SIGNAL_KEYS = ("database_tables", "project_configs", "architecture_flows", "business_calibers")
# The object entries: the field that names the subject, and the list fields that describe it.
_DOMAIN_OBJECT_FIELDS = {
    "database_tables": ("table", ("verified_fields", "enums", "join_keys", "pitfalls")),
    "project_configs": ("topic", ("facts", "pitfalls")),
}
_DOMAIN_ENTRY_LIMIT = 20  # entries kept per key
_DOMAIN_ITEM_LIMIT = 12  # items kept per list field
_DOMAIN_TEXT_LIMIT = 160  # characters kept per text item


def empty_domain_signals() -> dict[str, list[Any]]:
    """The `domain_signals` of a finding that recorded no domain clue."""
    return {key: [] for key in DOMAIN_SIGNAL_KEYS}


def _signal_text(value: Any) -> str:
    """One text item of a signal, trimmed and with any credential shape masked."""
    return mask_credential_shapes(_as_text(value))[:_DOMAIN_TEXT_LIMIT].strip()


def _signal_list(raw: Any) -> list[str]:
    """A list field of a signal: its text items, deduplicated, in order."""
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    return list(dict.fromkeys(text for text in map(_signal_text, items) if text))[:_DOMAIN_ITEM_LIMIT]


def _domain_signal_entry(key: str, entry: Any) -> Any:
    """One normalised entry of a key, or None when it names no subject."""
    if key not in _DOMAIN_OBJECT_FIELDS:
        return _signal_text(entry) or None
    name_field, list_fields = _DOMAIN_OBJECT_FIELDS[key]
    if isinstance(entry, str):
        entry = {name_field: entry}
    if not isinstance(entry, dict):
        return None
    name = _signal_text(entry.get(name_field))
    if not name:
        return None
    item: dict[str, Any] = {name_field: name}
    for field in list_fields:
        item[field] = _signal_list(entry.get(field))
    return item


def normalize_domain_signals(raw: Any) -> dict[str, list[Any]]:
    """The `domain_signals` of one finding, in the one shape a planner reads.

    The field is optional in the review contract, so whatever came back (nothing, a
    malformed value, other keys) yields the four keys. An entry that names no subject is
    dropped, and a text that carries a credential is masked: a password never travels
    from a review into a plan. Nothing here fails the review.
    """
    signals = empty_domain_signals()
    if not isinstance(raw, dict):
        return signals
    for key in DOMAIN_SIGNAL_KEYS:
        entries = raw.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            item = _domain_signal_entry(key, entry)
            if item is not None and item not in signals[key] and len(signals[key]) < _DOMAIN_ENTRY_LIMIT:
                signals[key].append(item)
    return signals


def _normalize_planned_write(
    raw_item: dict[str, Any],
    default_doc_prefix: str,
    *,
    skills_root: Path | None = None,
    notes: list[str] | None = None,
) -> PlannedWrite:
    """Deterministically normalize one validated plan item. Raises `_DropItem` when the item is unusable.

    `skills_root` is the directory the Skill writer actually writes to. When given, a Skill item whose action
    contradicts the state of its target directory is dropped (`update` on a missing target) or relabeled
    (`create` on an existing one), so the action shown for approval is the action the writer performs.
    """
    pw = PlannedWrite.model_validate(raw_item)

    # Privacy gate before anything durable happens: <private> spans are redacted and a
    # credential is refused here, so a poisoned draft never reaches SQLite, the approval
    # preview or the write path.
    try:
        pw.content_preview = sanitize_and_check_secrets(
            pw.content_preview, context=f"p03:{pw.target_kind}"
        )
    except SecretLeakError:
        raise _DropItem("SECRET_LEAK_BLOCKED") from None

    if pw.target_kind == "flywheel_rule":
        pw.action = "register_rule"
        pw.rule_payload = normalize_rule_payload(pw)
        try:
            payload = dict(pw.rule_payload)
            payload["title"] = sanitize_and_check_secrets(
                str(payload.get("title") or ""), context="rule:title"
            )
            payload["checklist"] = [
                sanitize_and_check_secrets(str(entry), context="rule:checklist")
                for entry in (payload.get("checklist") or [])
            ]
            pw.rule_payload = payload
        except SecretLeakError:
            raise _DropItem("SECRET_LEAK_BLOCKED") from None
        if not pw.content_preview:
            chk = pw.rule_payload.get("checklist", [])
            pw.content_preview = "\n".join(f"- {c}" for c in chk)
    elif pw.target_kind == "skill_improvement":
        slug = SkillWriterAdapter.extract_skill_name(pw.relative_path, fallback_title=pw.title)
        if not slug or len(slug) < 2:
            slug = _ascii_slug(pw.title, fallback="workflow-helper")
        pw.relative_path = f"{slug}/SKILL.md"
        if pw.action not in {"create", "update"}:
            pw.action = "create"
        body = pw.content_preview.strip()
        if not body:
            # No template prose: a Skill without model-written content is not a Skill.
            raise _DropItem("EMPTY_CONTENT")
        relabeled = False
        if skills_root is not None:
            target_exists = (skills_root / slug / "SKILL.md").exists()
            if pw.action == "update" and not target_exists:
                # The writer would turn this into a new directory labelled as an update.
                raise _DropItem(f"UPDATE_TARGET_MISSING: {slug}")
            if pw.action == "create" and target_exists:
                # The writer backs up and overwrites: that is an update, so show it as one.
                pw.action = "update"
                relabeled = True
                if notes is not None:
                    notes.append(f"CREATE_TARGET_EXISTS: {slug}")
        pw.requires_approval = True
        if pw.action == "update":
            # An update appends one section to the existing file: the old bytes stay untouched.
            existing_info: dict[str, Any] = {}
            if skills_root is not None:
                existing_info = inspect_skill_at(skills_root, slug)
                if not existing_info.get("exists"):
                    raise _DropItem(f"UPDATE_TARGET_MISSING: {slug}")
                if not existing_info.get("valid"):
                    raise _DropItem(f"UPDATE_TARGET_INVALID: {slug}")
            addition = normalize_update_addition(
                body,
                fallback_heading=pw.title or slug,
                today_ymd=shanghai_today_ymd(),
            )
            if not addition:
                raise _DropItem("EMPTY_CONTENT")
            if skills_root is not None and _addition_adds_nothing(addition, str(existing_info.get("content") or "")):
                # Everything the model wrote is already in the file: an update that changes nothing.
                raise _DropItem(f"UPDATE_NO_NEW_CONTENT: {slug}")
            # The preview the approver sees is the exact section the writer appends.
            pw.content_preview = addition
            if not pw.approval_reason:
                pw.approval_reason = "本批建议优化已有 Skill，需确认后更新技能文件"
            if relabeled:
                pw.approval_reason = _append_reason(pw.approval_reason, "目标 Skill 已存在，按更新处理")
            pw.approval_reason = _append_reason(
                pw.approval_reason,
                f"在现有文件末尾追加小节，原有 {int(existing_info.get('line_count') or 0)} 行完整保留，原文件自动备份",
            )
        else:
            # Ensure content_preview has valid SKILL.md YAML frontmatter
            if not body.startswith("---"):
                desc = (pw.summary or pw.title).replace("\n", " ").strip()[:180]
                pw.content_preview = f"---\nname: {slug}\ndescription: {desc}\n---\n\n{body}\n"
            if not pw.approval_reason:
                pw.approval_reason = "本批建议新建可复用 Skill，需确认后写入技能目录"
    elif pw.target_kind == "knowledge_doc":
        pw.title = _clean_title(pw.title, pw.summary or pw.content_preview)
        if not pw.relative_path:
            prefix = default_doc_prefix.replace("\\", "/").strip("/")
            today = shanghai_today_ymd()
            pw.relative_path = f"{prefix}/{today}-{_slugify(pw.title)}.md" if prefix else f"lessons/{today}-{_slugify(pw.title)}.md"
        if not pw.content_preview.strip():
            raise _DropItem("EMPTY_CONTENT")
        if pw.action in {"delete", "overwrite_core"}:
            pw.requires_approval = True
            pw.approval_reason = pw.approval_reason or "涉及高风险文档修改，需人工确认"
        page_type = str((pw.wiki_route or {}).get("page_type") or "").strip().lower() if isinstance(pw.wiki_route, dict) else ""
        if page_type == SENSITIVE_CONFIG_PAGE_TYPE and not pw.requires_approval:
            # The project configuration page is a sensitive write whatever the action: it is
            # flagged here, so the approval preview already says so before the Wiki gate routes it.
            pw.requires_approval = True
            pw.approval_reason = pw.approval_reason or "项目配置页属于敏感写入，需人工确认后才会落盘"
    return pw


def _validation_reasons(exc: ValidationError) -> list[str]:
    reasons: list[str] = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error.get("loc", ())) or "?"
        code = "FIELD_MISSING" if str(error.get("type", "")).startswith("missing") else "FIELD_INVALID"
        reason = f"{code}: {field}"
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _item_brief(raw_item: Any) -> dict[str, Any]:
    """Identifies a plan item without its body, for diagnostics, audit and logs."""
    if not isinstance(raw_item, dict):
        return {"target_kind": None, "action": None, "relative_path": None, "title": None}
    return {
        "target_kind": _as_text(raw_item.get("target_kind"))[:40] or None,
        "action": _as_text(raw_item.get("action"))[:40] or None,
        "relative_path": _as_text(raw_item.get("relative_path"))[:200] or None,
        "title": _as_text(raw_item.get("title"))[:80] or None,
    }


def _normalize_plan_item(
    raw_item: Any, *, default_doc_prefix: str, skills_root: Path | None
) -> tuple[PlannedWrite | None, list[str], list[str], list[str]]:
    """Normalize one planner-returned item into (write or None, drop reasons, notes, completed fields)."""
    if not isinstance(raw_item, dict):
        return None, ["NOT_AN_OBJECT"], [], []
    kind_raw = _as_text(raw_item.get("target_kind"))
    kind = kind_raw.lower()
    if kind and kind not in SUPPORTED_TARGET_KINDS:
        return None, [f"UNSUPPORTED_TARGET_KIND: {kind_raw[:40]}"], [], []

    item, completed = _complete_missing_fields(raw_item)
    if kind:
        item["target_kind"] = kind
    if "wiki_route" in item and not isinstance(item["wiki_route"], dict):
        # A malformed routing declaration counts as absent: routing then reports it as missing.
        item["wiki_route"] = None
    notes: list[str] = []
    try:
        pw = _normalize_planned_write(item, default_doc_prefix, skills_root=skills_root, notes=notes)
    except _DropItem as drop:
        return None, drop.reasons, notes, completed
    except ValidationError as exc:
        return None, _validation_reasons(exc), notes, completed
    except Exception as exc:  # one untrusted item must never crash the run: drop it and leave a trace
        return None, [f"NORMALIZE_ERROR: {type(exc).__name__}"], notes, completed
    return pw, [], notes, completed
