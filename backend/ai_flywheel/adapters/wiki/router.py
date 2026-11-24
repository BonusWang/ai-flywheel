"""Where a knowledge document belongs.

Routing is deterministic: the planner's declaration chooses the page type and the
project, and the evidence (the source session's workspace, the draft's own content)
only validates that declaration. Without a declaration only the dry-run planner's
evidence-based selection applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ai_flywheel.adapters.wiki.schema import normalize_relative_path, split_frontmatter
from ai_flywheel.config import KnowledgeSinkConfig

# Who decided the page type and the project of a knowledge document.
ROUTE_DECLARED = "declared"  # the planner declared them; evidence only validated the declaration
ROUTE_EVIDENCE = "evidence"  # dry-run planner has no declaration: workspace/content evidence selected
ROUTE_PATH = "path"          # draft already satisfies the schema: its own path decides

# Why a knowledge document is not written. `no_evidence` keeps the original wording.
UNROUTED_DOC_REASON = "无法归属到 Wiki 项目：来源会话工作区与草稿内容均无项目证据"

# What the reason codes mean, for the failure message.
UNROUTED_REASON_TEXT = {
    "declared_none": "模型声明无归属（跨项目或非项目实践）",
    "declaration_invalid": "路由声明不在挂载 Wiki 的候选集内",
    "declaration_missing": "模型执行器的知识文档计划缺少路由声明",
    "workspace_mismatch": "声明与来源会话工作区冲突",
    "content_mismatch": "声明与草稿内容冲突",
    "no_routable_target": "挂载 Wiki 没有可路由的页面类型或候选项目",
    "no_evidence": UNROUTED_DOC_REASON,
    "page_type_not_routable": "页面类型不属于可路由类型",
    "path_mismatch": "声明与目标路径所示项目不一致",
}
# Short label of every reason code, for node and audit summaries.
UNROUTED_REASON_LABEL = {
    "declared_none": "声明无归属",
    "declaration_invalid": "声明不在候选集",
    "declaration_missing": "缺少路由声明",
    "workspace_mismatch": "声明与工作区冲突",
    "content_mismatch": "声明与草稿冲突",
    "no_routable_target": "无可路由目标",
    "no_evidence": "无归属证据",
    "page_type_not_routable": "页面类型不可路由",
    "path_mismatch": "声明与目标路径冲突",
}
# Values of a declared project that mean "no attribution". Only these exact spellings count.
_NO_ATTRIBUTION_TOKENS = {"", "none", "null", "n/a", "na", "-", "无", "无归属", "不归属", "不适用"}
# Punctuation a planner may wrap around a declared identifier; stripped before the exact match.
_DECLARATION_WRAP_CHARS = "「」『』“”\"'`()（）[]【】<>《》:：;；,，、.。 \t"

# A project-name fragment must be distinctive to count as routing evidence.
_MIN_PROJECT_TOKEN_LEN = 3
_GENERIC_PROJECT_WORDS = {
    "admin", "api", "app", "apps", "base", "client", "common", "core", "data", "demo",
    "dev", "home", "main", "platform", "server", "service", "site", "system", "test",
    "the", "tool", "tools", "web", "work",
}
_LEADING_ASCII_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class RoutingCandidates:
    """Page types and projects a knowledge document may be routed to, read off the mounted Wiki."""

    page_types: list[str] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)

    @property
    def routable(self) -> bool:
        return bool(self.page_types and self.projects)

    def to_dict(self, *, description_limit: int = 0) -> dict[str, Any]:
        """Serialisable form; ``description_limit`` caps each project description (0 keeps it whole)."""
        return {
            "page_types": list(self.page_types),
            "projects": [
                {
                    "project": project["dir_name"],
                    "project_id": project["project_id"],
                    "aliases": list(project.get("aliases") or []),
                    "description": (project.get("description") or "")[:description_limit]
                    if description_limit
                    else project.get("description") or "",
                }
                for project in self.projects
            ],
        }


@dataclass
class RouteDecision:
    """Deterministic verdict on whether a knowledge document is written and where, with its basis."""

    routed: bool = False
    source: str = ""  # ROUTE_DECLARED | ROUTE_EVIDENCE | ROUTE_PATH; empty when not routed
    page_type: str = ""
    project: dict[str, Any] | None = None
    reason_code: str = ""  # key into UNROUTED_REASON_TEXT; empty when routed
    reason: str = ""  # the planner's own one-line reason, kept verbatim
    declared: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    detail: str = ""
    # The gate could not read the contract, so it says nothing about the document: leave it untouched
    # and let the write gate report the unreadable schema instead of unrouting everything quietly.
    passthrough: bool = False

    @property
    def project_dir(self) -> str:
        return str(self.project["dir_name"]) if self.project else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "routed": self.routed,
            "source": self.source,
            "page_type": self.page_type,
            "project": self.project_dir,
            "project_id": str((self.project or {}).get("project_id") or ""),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "declared": dict(self.declared),
            "evidence": {key: list(value) for key, value in self.evidence.items()},
            "detail": self.detail,
        }


@dataclass
class RouteContext:
    """What the routing verdict needs from the gate around it."""

    schema: dict[str, Any]
    candidates: RoutingCandidates
    projects_root_rel: str
    sink: KnowledgeSinkConfig
    #: The gate's own document check, so a draft that already satisfies the schema keeps its path.
    validate_document: Callable[[str, str], Any]


def _token_in_text(token: str, text: str) -> bool:
    """Match a lowercased project token; ASCII tokens must sit on alphanumeric boundaries."""
    if re.search(r"[一-鿿]", token):
        return token in text
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text) is not None


def _project_tokens(candidate: dict[str, Any]) -> list[str]:
    """Distinctive names of a Wiki project usable as routing evidence (most specific first)."""
    dir_name = candidate["dir_name"]
    leading = _LEADING_ASCII_RE.match(dir_name)
    tokens: list[str] = []
    for token in (candidate["project_id"].lower(), dir_name.lower(), leading.group(0).lower() if leading else ""):
        if len(token) >= _MIN_PROJECT_TOKEN_LEN and token not in tokens and token not in _GENERIC_PROJECT_WORDS:
            tokens.append(token)
    return tokens


def _index_aliases(raw: Any) -> list[str]:
    """Alternate names of a project as declared on its practice index page."""
    if isinstance(raw, list):
        items = [str(item) for item in raw]
    elif isinstance(raw, str):
        items = re.split(r"[,，;；、|]+", raw)
    else:
        items = []
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))[:8]


def _candidate_keys(candidate: dict[str, Any]) -> set[str]:
    """Lower-cased identifiers a planner may use to name this project exactly."""
    keys = {candidate["dir_name"].strip().lower(), str(candidate.get("project_id") or "").strip().lower()}
    keys.update(str(alias).strip().lower() for alias in candidate.get("aliases") or [])
    keys.discard("")
    return keys


def _clean_identifier(raw: Any) -> str:
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    return text.strip().strip(_DECLARATION_WRAP_CHARS)


def _declared_route(raw: Any) -> dict[str, str]:
    """The planner's routing declaration, whitespace-normalised but otherwise untouched."""
    if not isinstance(raw, dict):
        return {}
    return {
        "page_type": _clean_identifier(raw.get("page_type")).lower(),
        "project": _clean_identifier(raw.get("project")),
        "reason": " ".join(str(raw.get("reason") or "").split())[:120],
    }


def _workspace_segments(workspaces: list[str]) -> list[str]:
    return [
        segment.lower()
        for workspace in workspaces
        for segment in re.split(r"[\\/]+", str(workspace or ""))
        if segment.strip()
    ]


def _declaration_conflict(
    project_dir: str, workspace_hits: list[str], content_hits: list[str]
) -> tuple[str, str] | None:
    """How the evidence contradicts a routed project, or None when it does not.

    Workspace evidence, when present, settles the question; draft mentions only count when no
    workspace matched. Empty evidence is not a contradiction.
    """
    if workspace_hits and project_dir not in workspace_hits:
        return "workspace_mismatch", f"声明 {project_dir}，来源会话工作区命中 {'、'.join(workspace_hits)}"
    if not workspace_hits and content_hits and project_dir not in content_hits:
        return "content_mismatch", f"声明 {project_dir}，草稿命中 {'、'.join(content_hits)}"
    return None


def routable_page_types(schema: dict[str, Any], card_body_mappers: dict[str, Any]) -> list[str]:
    """Schema page types this adapter can materialise and whose partition the schema resolves.

    Whether the sink is allowed to write into that partition is a configuration concern: a
    mismatch there fails loudly in P04 instead of quietly unrouting every document here.
    """
    declared = (schema.get("content_contract") or {}).get("page_types") or {}
    partitions = (schema.get("partition_contract") or {}).get("partitions") or {}
    routable: list[str] = []
    for page_type in card_body_mappers:
        type_cfg = declared.get(page_type) if isinstance(declared, dict) else None
        partition_id = str(type_cfg.get("partition") or "") if isinstance(type_cfg, dict) else ""
        partition_cfg = partitions.get(partition_id) if isinstance(partitions, dict) else None
        root = str(partition_cfg.get("root") or "") if isinstance(partition_cfg, dict) else ""
        if root.strip("/"):
            routable.append(page_type)
    return routable


def discover_practice_projects(projects_root: Path) -> list[dict[str, Any]]:
    """Dynamically inspect mounted Wiki project dirs that host a 实战笔记/ folder (zero hardcoded names).

    Each candidate carries the identifiers and the one-line description declared on its
    practice index page, so a planner can name it exactly and judge whether a note is about it.
    """
    if not projects_root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("archive-"):
            continue
        practice_dir = child / "实战笔记"
        if not practice_dir.is_dir():
            continue
        candidate: dict[str, Any] = {
            "dir_name": child.name,
            "project_id": child.name,
            "aliases": [],
            "description": "",
        }
        index_file = practice_dir / "笔记索引.md"
        if index_file.is_file():
            fm_text, _ = split_frontmatter(index_file.read_text(encoding="utf-8", errors="ignore"))
            if fm_text:
                try:
                    fm = yaml.safe_load(fm_text) or {}
                except yaml.YAMLError:
                    fm = {}
                if isinstance(fm, dict):
                    if fm.get("project_id"):
                        candidate["project_id"] = str(fm["project_id"]).strip()
                    candidate["aliases"] = _index_aliases(fm.get("aliases"))
                    candidate["description"] = " ".join(str(fm.get("description") or "").split())
        found.append(candidate)
    return found


def evidence_hits(
    candidates: list[dict[str, Any]], haystack: str, workspaces: list[str]
) -> tuple[list[str], list[str]]:
    """Project directory names hit by workspace evidence and by the draft content.

    Workspace hits keep exact folder matches ahead of containing folders, in path order.
    """
    exact: list[str] = []
    contains: list[str] = []
    for segment in _workspace_segments(workspaces):
        for candidate in candidates:
            name = candidate["dir_name"]
            if segment in (name.lower(), str(candidate["project_id"]).lower()):
                if name not in exact:
                    exact.append(name)
            elif name not in contains and any(_token_in_text(token, segment) for token in _project_tokens(candidate)):
                contains.append(name)
    content = [
        candidate["dir_name"]
        for candidate in candidates
        if any(_token_in_text(token, haystack) for token in _project_tokens(candidate))
    ]
    return list(dict.fromkeys(exact + contains)), content


def project_of_path(
    rel: str, candidates: list[dict[str, Any]], projects_root_rel: str
) -> dict[str, Any] | None:
    """The project a path names, when the path sits under that project's directory."""
    lowered = rel.lower()
    for candidate in candidates:
        if lowered.startswith(f"{projects_root_rel.lower()}/{candidate['dir_name'].lower()}/"):
            return candidate
    return None


def declared_project(
    project: str, candidates: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """Resolve a declared project to exactly one candidate: ``none`` | ``invalid`` | ``ok``."""
    if project.lower() in _NO_ATTRIBUTION_TOKENS:
        return "none", None
    matches = [candidate for candidate in candidates if project.lower() in _candidate_keys(candidate)]
    if len(matches) != 1:
        return "invalid", None
    return "ok", matches[0]


def select_practice_project(
    candidates: list[dict[str, Any]],
    haystack: str,
    workspaces: list[str],
    *,
    fallback_project: str = "",
) -> dict[str, Any] | None:
    """Pick the Wiki project a card belongs to on evidence only (dry-run planner plans).

    Workspace paths of the source sessions come first, then project names found in the
    draft. Without any evidence the result is None: a card is never guessed into a project.
    """
    if not candidates:
        return None
    workspace_hits, _ = evidence_hits(candidates, haystack, workspaces)
    if workspace_hits:  # a workspace folder that names the project
        return next(c for c in candidates if c["dir_name"] == workspace_hits[0])

    best: tuple[dict[str, Any], int] | None = None
    for candidate in candidates:  # the draft itself names the project
        for token in _project_tokens(candidate):
            if _token_in_text(token, haystack):
                if best is None or len(token) > best[1]:
                    best = (candidate, len(token))
                break
    if best is not None:
        return best[0]

    fallback = (fallback_project or "").strip().lower()
    if fallback:
        for candidate in candidates:
            if candidate["dir_name"].lower() == fallback:
                return candidate
    return None


def evaluate_route(
    *,
    context: RouteContext,
    relative_path: str | None,
    title: str,
    summary: str,
    content: str,
    wiki_route: dict[str, Any] | None = None,
    source_workspaces: list[str] | None = None,
    project_hint: str = "",
    allow_evidence_fallback: bool = True,
    candidates: RoutingCandidates | None = None,
) -> RouteDecision:
    """Decide, without any model call, whether a knowledge document is written and where.

    The planner's declaration (``wiki_route``) chooses the page type and the project; workspace
    and draft evidence only validate it. Without a declaration only the dry-run planner's
    evidence-based selection applies, and only when ``allow_evidence_fallback`` is set.
    """
    candidates = candidates if candidates is not None else context.candidates
    declared = _declared_route(wiki_route)
    has_declaration = isinstance(wiki_route, dict)
    rel = normalize_relative_path(relative_path or "")
    workspaces = source_workspaces or []
    haystack = f"{project_hint} {rel} {title} {summary} {content}".lower()
    workspace_hits, content_hits = evidence_hits(candidates.projects, haystack, workspaces)
    evidence = {"workspace": workspace_hits, "content": content_hits}

    def verdict(
        routed: bool,
        *,
        code: str = "",
        detail: str = "",
        source: str = "",
        page_type: str = "",
        project: dict[str, Any] | None = None,
    ) -> RouteDecision:
        return RouteDecision(
            routed=routed,
            source=source if routed else "",
            page_type=page_type if routed else "",
            project=project if routed else None,
            reason_code=code,
            reason=declared.get("reason", ""),
            declared=declared,
            evidence=evidence,
            detail=detail or UNROUTED_REASON_TEXT.get(code, ""),
        )

    # 1. A draft that already satisfies the schema at its own path: the path decides.
    if rel:
        check = context.validate_document(rel, content)
        if check.ok:
            path_project = project_of_path(rel, candidates.projects, context.projects_root_rel)
            if check.page_type not in candidates.page_types:
                return verdict(
                    False, code="page_type_not_routable", detail=f"页面类型 {check.page_type} 不属于可路由类型"
                )
            if path_project is None:
                return verdict(False, code="no_routable_target")
            if not has_declaration:
                if not allow_evidence_fallback:
                    return verdict(False, code="declaration_missing")
                return verdict(True, source=ROUTE_PATH, page_type=check.page_type, project=path_project)
            state, declared_project_match = declared_project(declared["project"], candidates.projects)
            if state == "none":
                return verdict(False, code="declared_none")
            if state == "invalid":
                return verdict(False, code="declaration_invalid")
            if declared_project_match is not path_project:
                return verdict(
                    False,
                    code="path_mismatch",
                    detail=f"声明 {declared['project']}，目标路径所示项目为 {path_project['dir_name']}",
                )
            conflict = _declaration_conflict(path_project["dir_name"], workspace_hits, content_hits)
            if conflict:
                return verdict(False, code=conflict[0], detail=conflict[1])
            return verdict(True, source=ROUTE_DECLARED, page_type=check.page_type, project=path_project)

    # 2. Nothing to route to.
    if not candidates.routable:
        return verdict(False, code="no_routable_target")

    # 3. No declaration: only the dry-run planner's evidence-based selection may route.
    if not has_declaration:
        if not allow_evidence_fallback:
            return verdict(False, code="declaration_missing")
        chosen = select_practice_project(
            candidates.projects,
            haystack,
            workspaces,
            fallback_project=context.sink.practice_fallback_project,
        )
        if chosen is None:
            return verdict(False, code="no_evidence")
        return verdict(True, source=ROUTE_EVIDENCE, page_type=candidates.page_types[0], project=chosen)

    # 4. A declaration is validated, never corrected.
    state, declared_project_match = declared_project(declared["project"], candidates.projects)
    if state == "none":
        return verdict(False, code="declared_none")
    if declared["page_type"] not in candidates.page_types:
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的页面类型 {declared['page_type'] or '（空）'} 不在可路由类型内",
        )
    if declared_project_match is None:
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的项目 {declared['project'] or '（空）'} 不能唯一匹配候选项目",
        )
    name = declared_project_match["dir_name"]
    conflict = _declaration_conflict(name, workspace_hits, content_hits)
    if conflict:
        return verdict(False, code=conflict[0], detail=conflict[1])
    return verdict(True, source=ROUTE_DECLARED, page_type=declared["page_type"], project=declared_project_match)
