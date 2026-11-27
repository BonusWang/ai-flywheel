"""The write-time schema check.

A document is checked against the mounted Wiki's contract before it is written: its
frontmatter must be the first bytes of the file, its page type must exist, its fields
must stay inside the whitelist of that type, its dates must be real and not in the
future, and it must sit inside the partition the contract declares for that type.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import yaml

from ai_flywheel.adapters.wiki.schema import (
    FORBIDDEN_PATH_SEGMENTS,
    PROJECTS_PARTITION_ID,
    SENSITIVE_LOAD_POLICY,
    WikiGateError,
    deposit_routing_rules,
    normalize_relative_path,
    split_frontmatter,
)
from ai_flywheel.core.time_utils import shanghai_today_ymd

# `WikiGateError` is raised by the schema reader as well, so it is defined with the
# contract; it is re-exported here because the gate's errors belong to this module too.

ALLOWED_LOAD_POLICIES = {
    "always",
    "on_demand",
    "index_then_on_demand",
    "explicit_or_task_required",
}
PRACTICE_TYPES = {"project-practice-note", "project-practice-lite"}
PRACTICE_FIELD_WHITELIST = {
    "title",
    "description",
    "type",
    "project_id",
    "record_id",
    "event_date",
    "load_policy",
    "default_load",
    "created",
    "updated",
    "status",
    "tags",
    "sources",
    "related",
    "sensitivity",
    "freshness_tier",
    "last_verified",
    "valid_until",
}
MAX_DESCRIPTION_CHARS = 120
MAX_TAGS = 8
T5_MAX_BODY_LINES = 80
T5B_MAX_BODY_LINES = 20
T5_SECTIONS = ["好流程", "错误示范", "关键事项", "遗留与待办", "证据指针"]
T5B_SECTIONS = ["结论", "适用范围", "来源"]
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONDITION_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(==|!=)\s*(\S+)\s*$")


@dataclass
class WikiGateResult:
    applicable: bool = False
    ok: bool = True
    skipped_reason: str = ""
    schema_version: str = ""
    page_type: str = ""
    issues: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    topic_lint: dict[str, Any] | None = None
    postflight: dict[str, Any] | None = None
    artifact_path: str = ""

    def error(self, code: str, message: str, field_name: str = "") -> None:
        self.ok = False
        self.issues.append({"severity": "error", "code": code, "field": field_name, "message": message})

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "schema_version": self.schema_version,
            "page_type": self.page_type,
            "issues": self.issues,
            "warnings": self.warnings,
            "checks": self.checks,
            "topic_lint": self.topic_lint,
            "postflight": self.postflight,
            "artifact_path": self.artifact_path,
        }


def _strip_fenced(lines: list[str]) -> list[str]:
    """Drop fenced code block contents so headings inside code do not count."""
    kept: list[str] = []
    fence: str | None = None
    for line in lines:
        stripped = line.lstrip()
        if fence is None and stripped.startswith("```"):
            fence = stripped[:3]
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        kept.append(line)
    return kept


def _body_lines(body: str) -> list[str]:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _section_name(raw: str) -> str:
    return re.split(r"[（(]", raw, maxsplit=1)[0].strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _deposit_rules(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The contract's deposit routing for a document check; none when it is absent or unusable.

    A routing the schema reader already accepted is read again here because the check also
    runs on a contract handed over directly. One it cannot read is reported by the schema
    reader, not turned into a silent "no domain pages".
    """
    try:
        return deposit_routing_rules(schema)
    except WikiGateError:
        return []


def domain_page_issues(
    schema: dict[str, Any], relative_path: str, page_type: str, frontmatter: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Where a project-domain page departs from the deposit domain it belongs to.

    Only a Wiki that declares ``content_contract.deposit_routing`` has domain pages; for
    any other this says nothing. The domain is the one the page type routes to and whose
    directory the page sits in under its project: there the page follows the domain's
    templates, in placement, knowledge kind and load policy.
    """
    rules = [rule for rule in _deposit_rules(schema) if page_type in rule.get("page_types", [])]
    if not rules:
        return []
    partitions = (schema.get("partition_contract") or {}).get("partitions") or {}
    root_parts = [segment for segment in str((partitions.get(PROJECTS_PARTITION_ID) or {}).get("root") or "").split("/") if segment]
    parts = [segment for segment in normalize_relative_path(relative_path).split("/") if segment]
    # <projects root>/<project>/<domain directory>/<page>.md
    located = len(parts) >= len(root_parts) + 3 and parts[: len(root_parts)] == root_parts
    domain_dir = "/".join(parts[len(root_parts) + 1 : -1])
    domain = next((rule for rule in rules if located and rule.get("subdir") == domain_dir), None)
    if domain is None:
        where = " | ".join(f"{{project}}/{rule.get('subdir')}/" for rule in rules)
        return [
            (
                "DOMAIN_PLACEMENT",
                f"Page type '{page_type}' is deposited under {where} per content_contract.deposit_routing; "
                f"'{relative_path}' is not a page of those directories.",
                "type",
            )
        ]
    issues: list[tuple[str, str, str]] = []
    subdir = str(domain.get("subdir") or "")
    kinds = [str(kind) for kind in domain.get("knowledge_kinds") or []]
    if kinds and str(frontmatter.get("knowledge_kind") or "") not in kinds:
        issues.append(
            (
                "DOMAIN_KNOWLEDGE_KIND",
                f"Pages under {{project}}/{subdir}/ carry knowledge_kind {' or '.join(kinds)} "
                f"per their template; got '{frontmatter.get('knowledge_kind')}'.",
                "knowledge_kind",
            )
        )
    policies = [str(policy) for policy in domain.get("load_policies") or []]
    if policies and str(frontmatter.get("load_policy") or "") not in policies:
        issues.append(
            (
                "DOMAIN_LOAD_POLICY",
                f"Pages under {{project}}/{subdir}/ use load_policy {' or '.join(policies)} per their template; "
                f"got '{frontmatter.get('load_policy')}'.",
                "load_policy",
            )
        )
    if domain.get("sensitive") and (str(frontmatter.get("load_policy") or "") == SENSITIVE_LOAD_POLICY) and (
        "default_load" not in frontmatter or _as_bool(frontmatter.get("default_load"))
    ):
        issues.append(
            (
                "DOMAIN_DEFAULT_LOAD",
                f"A {domain.get('domain')} page is loaded only on explicit request: it requires default_load: false.",
                "default_load",
            )
        )
    return issues


def validate_body(
    result: WikiGateResult,
    page_type: str,
    fm: dict[str, Any],
    body: str,
) -> None:
    """The body skeleton of a page type: one H1, the declared H2 sections, the line budget."""
    lines = _body_lines(body)
    visible = _strip_fenced(lines)
    h1 = [m.group(1) for line in visible if (m := _H1_RE.match(line))]
    if len(h1) != 1:
        result.error("H1_COUNT", f"Body must contain exactly one H1; found {len(h1)}.")
        return
    title = str(fm.get("title") or "").strip()
    if title and re.sub(r"\s+", " ", title) != re.sub(r"\s+", " ", h1[0].strip()):
        result.warn(f"frontmatter title differs from H1: '{title}' vs '{h1[0].strip()}'")

    h2 = [_section_name(m.group(1)) for line in visible if (m := _H2_RE.match(line))]

    if page_type == "project-practice-note":
        if len(lines) > T5_MAX_BODY_LINES:
            result.error(
                "BODY_TOO_LONG",
                f"T5 body has {len(lines)} lines; hard limit is {T5_MAX_BODY_LINES}.",
            )
        h1_index = next(index for index, line in enumerate(visible) if _H1_RE.match(line))
        first_content = next((line.strip() for line in visible[h1_index + 1 :] if line.strip()), "")
        if not first_content.startswith("> 背景与结论："):
            result.error(
                "T5_HEAD_MISSING",
                "T5 requires the first body line after H1 to be '> 背景与结论：...'.",
            )
        tail_index = next(
            (index for index, line in enumerate(visible) if line.strip().startswith("> 分流判定：")),
            -1,
        )
        evidence_index = next(
            (
                index
                for index, line in enumerate(visible)
                if (m := _H2_RE.match(line)) and _section_name(m.group(1)) == "证据指针"
            ),
            -1,
        )
        if tail_index < 0:
            result.error(
                "T5_TAIL_MISSING",
                "T5 requires a trailing '> 分流判定：...' blockquote after 证据指针.",
            )
        elif tail_index < evidence_index:
            result.error(
                "T5_TAIL_ORDER",
                "T5 分流判定 must come after the 证据指针 section.",
            )
        if h2 != T5_SECTIONS:
            result.error(
                "T5_SECTIONS",
                f"T5 H2 sections must be exactly {T5_SECTIONS} in order; got {h2}.",
            )
    elif page_type == "project-practice-lite":
        if len(lines) > T5B_MAX_BODY_LINES:
            result.error(
                "BODY_TOO_LONG",
                f"T5b body has {len(lines)} lines; hard limit is {T5B_MAX_BODY_LINES}.",
            )
        if h2 != T5B_SECTIONS:
            result.error(
                "T5B_SECTIONS",
                f"T5b H2 sections must be exactly {T5B_SECTIONS} in order; got {h2}.",
            )
        h1_index = next(index for index, line in enumerate(visible) if _H1_RE.match(line))
        first_content = next((line.strip() for line in visible[h1_index + 1 :] if line.strip()), "")
        if not first_content.startswith("## 结论"):
            result.error(
                "T5B_HEAD_MISSING",
                "T5b requires the first body content after H1 to be '## 结论'.",
            )
        if any(line.strip().startswith("> 背景与结论：") for line in visible):
            result.error(
                "T5B_BACKGROUND_PRESENT",
                "T5b must not carry the T5 background blockquote; H1 is followed directly by 结论.",
            )
        if any(line.strip().startswith("> 分流判定：") for line in visible):
            result.error(
                "T5B_TAIL_PRESENT",
                "T5b must not carry the T5 分流判定 tail blockquote.",
            )


def validate_document(
    *,
    applicable: bool,
    schema_loader: Callable[[], dict[str, Any]],
    relative_path: str,
    content: str,
) -> WikiGateResult:
    """Check one document against the mounted contract.

    ``schema_loader`` reads the contract; it is a callable so the gate can hand over its
    cached loader. A contract that cannot be read is reported as `SCHEMA_UNREADABLE`
    instead of being silently ignored.
    """
    result = WikiGateResult(applicable=applicable)
    if not applicable:
        result.skipped_reason = "not_a_schema_governed_wiki_root"
        return result
    try:
        schema = schema_loader()
    except (OSError, WikiGateError, yaml.YAMLError) as exc:
        result.error("SCHEMA_UNREADABLE", str(exc))
        return result

    content_contract = schema["content_contract"]
    core_contract = schema["core_contract"]
    partition_contract = schema["partition_contract"]
    result.schema_version = str(content_contract.get("version", ""))
    rel = normalize_relative_path(relative_path)

    # 1. Core files are never direct-written by P04 (portal approval included).
    core_files = {str(name) for name in core_contract.get("core_files", [])}
    if rel in core_files or Path(rel).name in core_files:
        result.error(
            "CORE_FILE_BLOCKED",
            f"'{rel}' is a core file; P04 may not write core files even with portal approval.",
        )
        return result

    # 2. Path-level exclusions: raw / archive / process roots.
    segments = [seg for seg in rel.split("/") if seg]
    for seg in segments:
        if seg in FORBIDDEN_PATH_SEGMENTS or seg.startswith("archive-"):
            result.error(
                "FORBIDDEN_PATH",
                f"Path segment '{seg}' is excluded from P04 writes (raw/archive/process).",
            )
            return result

    # 3. Frontmatter must be the first bytes of the file.
    if content.startswith("\ufeff"):
        result.error("BOM_PRESENT", "File starts with a UTF-8 BOM; Wiki pages must not carry BOM.")
    fm_text, body = split_frontmatter(content)
    if fm_text is None:
        result.error(
            "FRONTMATTER_MISSING",
            "Frontmatter must start at line 1 with '---' and close with '---'; "
            "nothing (including a retirement banner) may precede it.",
        )
        return result
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        result.error("YAML_INVALID", f"Frontmatter YAML does not parse: {exc}")
        return result
    if not isinstance(fm, dict):
        result.error("FRONTMATTER_NOT_MAPPING", "Frontmatter must parse to a YAML mapping.")
        return result

    # 4. Page type contract.
    page_types = content_contract.get("page_types", {}) or {}
    legacy_aliases = content_contract.get("legacy_type_aliases", {}) or {}
    raw_type = str(fm.get("type") or "").strip()
    if not raw_type:
        result.error("FIELD_REQUIRED", "Missing required field 'type'.", "type")
        return result
    if raw_type in legacy_aliases:
        result.error(
            "LEGACY_TYPE_ALIAS",
            f"type '{raw_type}' is a legacy alias; new/touched pages must use "
            f"'{legacy_aliases[raw_type]}'.",
            "type",
        )
        return result
    if raw_type not in page_types:
        result.error("UNKNOWN_PAGE_TYPE", f"Unknown page type '{raw_type}'.", "type")
        return result
    type_cfg = page_types[raw_type] or {}
    result.page_type = raw_type

    # 5. Required fields (common + page type).
    required = [str(x) for x in (core_contract.get("required_fields") or [])]
    required += [str(x) for x in (type_cfg.get("required_fields") or [])]
    for name in dict.fromkeys(required):
        if fm.get(name) in (None, "", []):
            result.error("FIELD_REQUIRED", f"Missing required field '{name}'.", name)

    # 6. Practice-card whitelist and field limits.
    if raw_type in PRACTICE_TYPES:
        extras = sorted(set(fm) - PRACTICE_FIELD_WHITELIST)
        if extras:
            result.error(
                "FIELD_SUPERSET",
                f"Practice card fields must stay within the {len(PRACTICE_FIELD_WHITELIST)}-field whitelist; extra: {extras}",
            )
        description = str(fm.get("description") or "")
        if len(description) > MAX_DESCRIPTION_CHARS:
            result.error(
                "DESCRIPTION_TOO_LONG",
                f"description is {len(description)} chars; practice cards allow <= {MAX_DESCRIPTION_CHARS}.",
                "description",
            )
        tags = fm.get("tags")
        if tags is not None:
            if not isinstance(tags, list):
                result.error("TAGS_NOT_LIST", "tags must be a YAML list.", "tags")
            elif len(tags) > MAX_TAGS:
                result.error(
                    "TAGS_TOO_MANY",
                    f"tags has {len(tags)} entries; practice cards allow <= {MAX_TAGS}.",
                    "tags",
                )
    else:
        description = str(fm.get("description") or "")
        if len(description) > 200:
            result.warn(f"description is {len(description)} chars; consider <= 120 for retrieval.")

    # 7. Status and load policy enums.
    status = fm.get("status")
    allowed_status = [str(x) for x in (type_cfg.get("allowed_status") or [])]
    if status is not None and allowed_status and str(status) not in allowed_status:
        result.error(
            "STATUS_NOT_ALLOWED",
            f"status '{status}' is not allowed for type '{raw_type}' (allowed: {allowed_status}).",
            "status",
        )
    load_policy = fm.get("load_policy")
    if load_policy is not None and str(load_policy) not in ALLOWED_LOAD_POLICIES:
        result.error(
            "LOAD_POLICY_INVALID",
            f"load_policy '{load_policy}' is not one of {sorted(ALLOWED_LOAD_POLICIES)}.",
            "load_policy",
        )
    if type_cfg.get("load_policy") and load_policy not in (None, type_cfg["load_policy"]):
        result.error(
            "LOAD_POLICY_CONTRACT",
            f"type '{raw_type}' requires load_policy '{type_cfg['load_policy']}'.",
            "load_policy",
        )
    if raw_type in PRACTICE_TYPES:
        if str(load_policy) != "on_demand":
            result.error(
                "PRACTICE_LOAD_POLICY",
                "Practice cards require load_policy: on_demand.",
                "load_policy",
            )
        if "default_load" in fm and _as_bool(fm.get("default_load")):
            result.error(
                "PRACTICE_DEFAULT_LOAD",
                "Practice cards require default_load: false.",
                "default_load",
            )

    # 8. Dates must be real ISO dates and never in the future.
    today = date.fromisoformat(shanghai_today_ymd())
    for name in ("created", "updated", "event_date"):
        value = fm.get(name)
        if value in (None, ""):
            continue
        text_value = str(value)
        if not _DATE_RE.match(text_value):
            result.error("DATE_FORMAT", f"{name} must be YYYY-MM-DD, got '{text_value}'.", name)
            continue
        try:
            parsed = date.fromisoformat(text_value)
        except ValueError:
            result.error("DATE_INVALID", f"{name} '{text_value}' is not a valid date.", name)
            continue
        if parsed > today:
            result.error("DATE_IN_FUTURE", f"{name} '{text_value}' is after today {today}.", name)

    # 9. Conditional and value-constrained fields.
    for field_name, rule in (type_cfg.get("conditional_required_fields") or {}).items():
        condition = str((rule or {}).get("required_when") or "")
        match = _CONDITION_RE.match(condition)
        if not match:
            continue
        other, operator, expected = match.groups()
        actual = str(fm.get(other, "")).strip()
        applies = (actual == expected) if operator == "==" else (actual != expected)
        if applies and fm.get(field_name) in (None, ""):
            result.error(
                "CONDITIONAL_FIELD_REQUIRED",
                f"'{field_name}' is required when {condition}.",
                field_name,
            )
    for field_name, key in (
        ("knowledge_kind", "knowledge_kind_values"),
        ("doc_kind", "doc_kind_values"),
    ):
        allowed = [str(x) for x in (type_cfg.get(key) or [])]
        if allowed and fm.get(field_name) not in (None, "") and str(fm.get(field_name)) not in allowed:
            result.error(
                "FIELD_VALUE_NOT_ALLOWED",
                f"{field_name} '{fm.get(field_name)}' not in {allowed}.",
                field_name,
            )

    # 10. Placement inside the declared partition and path pattern.
    partition_id = str(type_cfg.get("partition") or "")
    partitions = partition_contract.get("partitions", {}) or {}
    partition_root = str((partitions.get(partition_id) or {}).get("root") or "")
    if not partition_id or not partition_root:
        result.error(
            "PARTITION_UNKNOWN",
            f"Page type '{raw_type}' declares no resolvable partition.",
            "type",
        )
        return result
    if rel != partition_root and not rel.startswith(partition_root + "/"):
        result.error(
            "PLACEMENT_OUTSIDE_PARTITION",
            f"'{rel}' is outside partition '{partition_id}' root '{partition_root}'.",
        )
        return result
    rel_in_partition = rel[len(partition_root) :].lstrip("/")
    path_pattern = type_cfg.get("path_pattern")
    file_pattern = type_cfg.get("file_pattern")
    if path_pattern and not re.fullmatch(str(path_pattern), rel_in_partition):
        result.error(
            "PATH_PATTERN_MISMATCH",
            f"'{rel_in_partition}' does not match '{path_pattern}' for type '{raw_type}'.",
        )
    file_name = Path(rel_in_partition).name
    if file_pattern and not re.fullmatch(str(file_pattern), file_name):
        result.error("FILE_PATTERN_MISMATCH", f"'{file_name}' does not match '{file_pattern}' for type '{raw_type}'.")
    if raw_type in PRACTICE_TYPES and re.search(r"/实战笔记/(原文|提炼)/", "/" + rel):
        result.error(
            "LEGACY_DOUBLE_LAYER",
            "New/touched practice cards must be single-layer under 实战笔记/; "
            "原文/提炼 double layer is legacy-only.",
        )

    # 11. Private credential pages stay in the approved project config vault: the directory the
    #     engine knows, plus any domain the contract's deposit routing marks sensitive.
    sensitivity = str(fm.get("sensitivity") or "").lower()
    contains_credentials = _as_bool(fm.get("contains_credentials"))
    if sensitivity == "private_sensitive" or contains_credentials:
        vaults = {"/项目配置/", *(f"/{rule['subdir']}/" for rule in _deposit_rules(schema) if rule.get("sensitive"))}
        if not any(vault in "/" + rel for vault in vaults):
            result.error(
                "CREDENTIAL_PAGE_PLACEMENT",
                "private_sensitive / contains_credentials pages must live under {project}/项目配置/.",
            )
        if str(load_policy) != "explicit_or_task_required":
            result.error(
                "CREDENTIAL_LOAD_POLICY",
                "Credential pages require load_policy: explicit_or_task_required.",
                "load_policy",
            )
        if "default_load" not in fm or _as_bool(fm.get("default_load")):
            result.error(
                "CREDENTIAL_DEFAULT_LOAD",
                "Credential pages require default_load: false.",
                "default_load",
            )
        if not fm.get("access_scope"):
            result.error("CREDENTIAL_ACCESS_SCOPE", "Credential pages require access_scope.", "access_scope")

    # 11b. A project-domain page follows the template of the deposit domain it is filed under.
    for code, message, field_name in domain_page_issues(schema, rel, raw_type, fm):
        result.error(code, message, field_name)

    # 12. Body skeleton and line budget.
    validate_body(result, raw_type, fm, body)
    result.checks.append("schema_frontmatter")
    result.checks.append("body_skeleton")
    return result
