from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.config import FlywheelConfig

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
}
FORBIDDEN_PATH_SEGMENTS = {"raw", "raw-原始素材", ".wiki-tmp", ".wiki-agent-memory"}
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


class WikiGateError(ValueError):
    """Raised when a P04 write is blocked by the schema-aware Wiki gate."""


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


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_yaml_text, body).  None means frontmatter is absent."""
    if content.startswith("﻿"):
        content = content[1:]
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, content


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


class WikiGate:
    """Contract-driven pre-write and post-write gate for a schema-governed Wiki."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg
        self.sink = cfg.knowledge_sink
        self._schema_cache: tuple[float, dict[str, Any]] | None = None

    @property
    def root(self) -> Path:
        return self.cfg.knowledge_root_path

    @property
    def schema_path(self) -> Path:
        return self.root / ".wiki-schema.md"

    @property
    def applicable(self) -> bool:
        return bool(self.sink.wiki_gate_enabled) and self.schema_path.is_file()

    def _load_schema(self) -> dict[str, Any]:
        stat = self.schema_path.stat()
        if self._schema_cache and self._schema_cache[0] == stat.st_mtime:
            return self._schema_cache[1]
        text = self.schema_path.read_text(encoding="utf-8", errors="replace")
        fm_text, _ = split_frontmatter(text)
        if fm_text is None:
            raise WikiGateError(f"{self.schema_path} has no parseable frontmatter")
        data = yaml.safe_load(fm_text)
        if not isinstance(data, dict):
            raise WikiGateError(f"{self.schema_path} frontmatter is not a mapping")
        for key in ("core_contract", "partition_contract", "content_contract"):
            if not isinstance(data.get(key), dict):
                raise WikiGateError(f"{self.schema_path} is missing machine block '{key}'")
        self._schema_cache = (stat.st_mtime, data)
        return data

    @staticmethod
    def _normalize(relative_path: str) -> str:
        rel = relative_path.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        return rel.lstrip("/")

    def validate_document(self, relative_path: str, content: str) -> WikiGateResult:
        result = WikiGateResult(applicable=self.applicable)
        if not self.applicable:
            result.skipped_reason = "not_a_schema_governed_wiki_root"
            return result
        try:
            schema = self._load_schema()
        except (OSError, WikiGateError, yaml.YAMLError) as exc:
            result.error("SCHEMA_UNREADABLE", str(exc))
            return result

        content_contract = schema["content_contract"]
        core_contract = schema["core_contract"]
        partition_contract = schema["partition_contract"]
        result.schema_version = str(content_contract.get("version", ""))
        rel = self._normalize(relative_path)

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
        if content.startswith("﻿"):
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
                    f"Practice card fields must stay within the 15-field whitelist; extra: {extras}",
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
        today = date.today()
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
        if file_pattern and not re.fullmatch(str(file_pattern), Path(rel_in_partition).name):
            result.error(
                "FILE_PATTERN_MISMATCH",
                f"'{Path(rel_in_partition).name}' does not match '{file_pattern}' for type '{raw_type}'.",
            )
        if raw_type in PRACTICE_TYPES and re.search(r"/实战笔记/(原文|提炼)/", "/" + rel):
            result.error(
                "LEGACY_DOUBLE_LAYER",
                "New/touched practice cards must be single-layer under 实战笔记/; "
                "原文/提炼 double layer is legacy-only.",
            )

        # 11. Private credential pages stay in the approved project config vault.
        sensitivity = str(fm.get("sensitivity") or "").lower()
        contains_credentials = _as_bool(fm.get("contains_credentials"))
        if sensitivity == "private_sensitive" or contains_credentials:
            if "/项目配置/" not in "/" + rel:
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

        # 12. Body skeleton and line budget.
        self._validate_body(result, raw_type, fm, body)
        result.checks.append("schema_frontmatter")
        result.checks.append("body_skeleton")
        return result

    def _discover_practice_projects(self, projects_root: Path) -> list[dict[str, str]]:
        """Dynamically inspect mounted Wiki project dirs that host a 实战笔记/ folder (zero hardcoded names)."""
        if not projects_root.is_dir():
            return []
        found: list[dict[str, str]] = []
        for child in sorted(projects_root.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name.startswith("archive-"):
                continue
            practice_dir = child / "实战笔记"
            if not practice_dir.is_dir():
                continue
            project_id = child.name
            index_file = practice_dir / "笔记索引.md"
            if index_file.is_file():
                fm_text, _ = split_frontmatter(index_file.read_text(encoding="utf-8", errors="ignore"))
                if fm_text:
                    try:
                        fm = yaml.safe_load(fm_text) or {}
                        if isinstance(fm, dict) and fm.get("project_id"):
                            project_id = str(fm["project_id"]).strip()
                    except yaml.YAMLError:
                        pass
            found.append({"dir_name": child.name, "project_id": project_id})
        return found

    def normalize_planned_document(
        self,
        relative_path: str | None,
        title: str,
        summary: str,
        content: str,
        *,
        source_sessions: list[str] | None = None,
        project_hint: str = "",
    ) -> tuple[str, str]:
        """Adapt a generic P03 knowledge_doc into a schema-compliant Wiki page (T5 project-practice-note) when WikiGate applies."""
        rel = self._normalize(relative_path or "")
        if not self.applicable:
            return rel, content
        try:
            schema = self._load_schema()
        except Exception:
            return rel, content

        # If already valid against the mounted schema, keep as-is
        if rel and self.validate_document(rel, content).ok:
            return rel, content

        partition_contract = schema.get("partition_contract") or {}
        partitions = partition_contract.get("partitions") or {}
        projects_root_rel = str((partitions.get("projects") or {}).get("root") or "wiki-知识库/projects-项目").strip("/")
        projects_root = self.root / projects_root_rel
        candidates = self._discover_practice_projects(projects_root)

        haystack = f"{project_hint} {rel} {title} {summary} {content[:600]}".lower()
        chosen = None
        for cand in candidates:
            tokens = [cand["dir_name"].lower(), cand["project_id"].lower()]
            tokens.extend(seg.lower() for seg in re.split(r"[-_\s]+", cand["dir_name"]) if len(seg) >= 2)
            if any(tok and tok in haystack for tok in tokens):
                chosen = cand
                break
        if chosen is None and candidates:
            # Prefer a general/sedimentation/knowledge practice directory if present, else first available
            chosen = next(
                (c for c in candidates if any(k in c["dir_name"].lower() for k in ("沉淀", "flywheel", "general", "ai"))),
                candidates[0],
            )
        dir_name = chosen["dir_name"] if chosen else "general"
        project_id = chosen["project_id"] if chosen else "general"

        today_str = date.today().isoformat()
        clean_title = re.sub(r"[\r\n]+", " ", (title or "协作实战经验卡")).strip()[:64] or "协作实战经验卡"
        file_slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", clean_title).strip("-").lower()[:40] or "practice-note"
        rec_slug = re.sub(r"[^a-z0-9]+", "-", clean_title.lower()).strip("-")[:24]
        if len(rec_slug) < 3:
            rec_slug = hashlib.sha1(clean_title.encode("utf-8")).hexdigest()[:10]
        record_id = f"{today_str}-{rec_slug}"
        target_rel = f"{projects_root_rel}/{dir_name}/实战笔记/{today_str}-{file_slug}.md"

        # Parse any existing frontmatter tags & body facts from the draft content
        fm_text, raw_body = split_frontmatter(content or "")
        draft_fm: dict[str, Any] = {}
        if fm_text:
            try:
                loaded = yaml.safe_load(fm_text)
                if isinstance(loaded, dict):
                    draft_fm = loaded
            except yaml.YAMLError:
                draft_fm = {}
        raw_tags = draft_fm.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()][:6]
        elif isinstance(raw_tags, str) and raw_tags.strip():
            tags = [t.strip() for t in re.split(r"[,，\s]+", raw_tags) if t.strip()][:6]
        if not tags:
            tags = ["实战复盘", "协作飞轮"]

        desc_raw = re.sub(r"[\r\n]+", " ", (summary or clean_title)).strip()
        description = desc_raw[:MAX_DESCRIPTION_CHARS]

        # Extract concise non-heading content lines from raw_body so no LLM insight is lost
        body_bullets: list[str] = []
        for line in (raw_body or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("---"):
                continue
            s = s.lstrip("-*• ").strip()
            if s and s not in body_bullets:
                body_bullets.append(s[:180])
            if len(body_bullets) >= 8:
                break
        if not body_bullets:
            body_bullets = [description]

        sources_list = [f"ai-flywheel session: {sk}" for sk in (source_sessions or []) if sk][:5]
        if not sources_list:
            sources_list = [f"ai-flywheel batch {today_str}"]

        fm_obj = {
            "title": clean_title,
            "description": description,
            "type": "project-practice-note",
            "project_id": project_id,
            "record_id": record_id,
            "event_date": today_str,
            "load_policy": "on_demand",
            "default_load": False,
            "created": today_str,
            "updated": today_str,
            "status": "active",
            "tags": tags[:MAX_TAGS],
            "sources": sources_list,
        }
        fm_yaml = yaml.safe_dump(fm_obj, allow_unicode=True, sort_keys=False).strip()

        good_flow_lines = [f"- {b}" for b in body_bullets[:3]]
        key_lines = [f"- {b}" for b in body_bullets[3:6]]
        key_lines.extend(
            [
                "- 适用前提与失效条件：适用于当前项目与工具链配置；若接口、分支策略或前置环境变更需重新核验。",
                "- 分歧与反例：暂未记录反例；遇例外场景请追加记录，勿直接覆盖原结论。",
            ]
        )
        evidence_lines = [f"- `{s}`" for s in sources_list]

        body_parts = [
            f"# {clean_title}",
            "",
            f"> 背景与结论：{description}",
            "",
            "## 好流程",
            *good_flow_lines,
            "",
            "## 错误示范",
            "- 跳过前置核查或凭记忆沿用过期方案，未保留真实运行验证证据。",
            "",
            "## 关键事项",
            *key_lines,
            "",
            "## 遗留与待办",
            "- 后续在同类真实任务中复测验证，通过后自动续期本经验卡。",
            "",
            "## 证据指针",
            *evidence_lines,
            "",
            "> 分流判定：单项目/单事件实战经验卡，按 T5 单层规范沉淀；跨项目稳定规律经复测后再升格为主题页或全局规则。",
            "",
        ]
        normalized_md = f"---\n{fm_yaml}\n---\n\n" + "\n".join(body_parts)
        return target_rel, normalized_md

    def _validate_body(
        self,
        result: WikiGateResult,
        page_type: str,
        fm: dict[str, Any],
        body: str,
    ) -> None:
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

    # ---------------- post-write validation ----------------

    def run_topic_lint(self, result: WikiGateResult, relative_path: str) -> None:
        if result.page_type != "topic":
            result.topic_lint = {"status": "skipped", "reason": "not_a_topic_card"}
            return
        script = self.sink.wiki_lint_script.strip()
        if not script:
            if self.sink.wiki_gate_require_scripts:
                result.error("LINT_SCRIPT_MISSING", "wiki_lint_script is not configured for topic cards.")
            else:
                result.topic_lint = {"status": "skipped", "reason": "lint_script_not_configured"}
            return
        target = self.root / relative_path
        python = self.sink.wiki_python.strip() or sys.executable
        command = [python, script, str(self.root), "--topic-card", str(target), "--change-state", "touched"]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.sink.wiki_gate_timeout_seconds,
                cwd=str(self.root),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.error("LINT_FAILED", f"wiki-lint could not run: {exc}")
            result.topic_lint = {"status": "error", "error": str(exc)}
            return
        payload = _last_json_line(proc.stdout) or {}
        result.topic_lint = {
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "report": payload,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-1000:],
        }
        if proc.returncode != 0:
            result.error(
                "LINT_FAILED",
                f"wiki-lint rejected the topic card (rc={proc.returncode}).",
            )

    def run_postflight(self, result: WikiGateResult, relative_path: str) -> None:
        script = self.sink.wiki_d2_postflight_script.strip()
        if not script:
            if self.sink.wiki_gate_require_scripts:
                result.error("D2_SCRIPT_MISSING", "wiki_d2_postflight_script is not configured.")
            else:
                result.postflight = {"status": "skipped", "reason": "d2_script_not_configured"}
            return
        evidence_dir = self.cfg.wiki_gate_evidence_path
        evidence_dir.mkdir(parents=True, exist_ok=True)
        json_out = evidence_dir / (
            "d2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        )
        python = self.sink.wiki_python.strip() or sys.executable
        command = [
            self.sink.wiki_bash or "bash",
            script,
            "--wiki-root",
            str(self.root),
            "--write-set",
            relative_path,
            "--today",
            date.today().isoformat(),
            "--json-out",
            str(json_out),
        ]
        env = os.environ.copy()
        env["PYTHON_BIN"] = python
        env["AI_HUB_WORK_ROOT"] = str(evidence_dir / "d2-work")
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.sink.wiki_gate_timeout_seconds,
                cwd=str(self.root),
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.error("D2_FAILED", f"wiki_d2_postflight.sh could not run: {exc}")
            result.postflight = {"status": "error", "error": str(exc)}
            return
        diagnostic = _last_json_line(proc.stdout) or {}
        if not diagnostic and json_out.is_file():
            try:
                diagnostic = json.loads(json_out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                diagnostic = {}
        status = "pass" if proc.returncode == 0 else ("blocked" if proc.returncode == 3 else "fail")
        result.postflight = {
            "status": status,
            "returncode": proc.returncode,
            "diagnostic": diagnostic,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-2000:],
            "evidence": str(json_out),
        }
        if proc.returncode != 0:
            detail = ""
            validator = diagnostic.get("validator") or {}
            if validator:
                detail = f" validator errors={validator.get('errors')} codes={validator.get('issue_codes')}"
            result.error(
                "D2_BLOCKED" if proc.returncode == 3 else "D2_FAILED",
                f"D2 postflight did not pass (rc={proc.returncode}).{detail}",
            )

    def resync_after_rollback(self, relative_path: str) -> dict[str, Any]:
        """Re-run index sync only after a failed write has been rolled back."""
        script = self.sink.wiki_d2_postflight_script.strip()
        if not script:
            return {"status": "skipped", "reason": "d2_script_not_configured"}
        python = self.sink.wiki_python.strip() or sys.executable
        command = [
            self.sink.wiki_bash or "bash",
            script,
            "--wiki-root",
            str(self.root),
            "--write-set",
            relative_path,
            "--today",
            date.today().isoformat(),
            "--skip-validator",
        ]
        env = os.environ.copy()
        env["PYTHON_BIN"] = python
        env["AI_HUB_WORK_ROOT"] = str(self.cfg.wiki_gate_evidence_path / "d2-work")
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.sink.wiki_gate_timeout_seconds,
                cwd=str(self.root),
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-1000:],
        }

    def write_artifact(self, result: WikiGateResult, relative_path: str, extra: dict[str, Any]) -> None:
        evidence_dir = self.cfg.wiki_gate_evidence_path
        evidence_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
        path = evidence_dir / (
            "gate-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "-"
            + digest
            + ".json"
        )
        payload = {
            "target": relative_path,
            "root": str(self.root),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "gate": result.to_dict(),
            **extra,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            result.artifact_path = str(path)
        except OSError:
            result.artifact_path = ""


def _last_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}
