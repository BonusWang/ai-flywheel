"""Where a knowledge document belongs.

Routing is deterministic: the planner's declaration chooses the page type and the
project, and the evidence (the source session's workspace, the draft's own content)
only validates that declaration. Without a declaration only the dry-run planner's
evidence-based selection applies.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.wiki.domain_pages import (
    build_domain_page,
    is_single_table_name,
    merge_domain_increment,
)
from ai_flywheel.adapters.wiki.routing_models import (
    CREATE_TARGET_EXISTS,
    DOMAIN_INVENTORY_LIMIT,
    INCIDENT_FILE_NAME_RE,
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
    ROUTE_PATH,
    SECRET_LEAK_BLOCKED,
    UNROUTED_DOC_REASON,
    UNROUTED_REASON_LABEL,
    UNROUTED_REASON_TEXT,
    UPDATE_NO_NEW_CONTENT,
    DepositRouting,
    IncidentRouting,
    RouteContext,
    RouteDecision,
    RoutingCandidates,
    _DECLARATION_WRAP_CHARS,
    _GENERIC_PROJECT_WORDS,
    _LEADING_ASCII_RE,
    _MIN_PROJECT_TOKEN_LEN,
    _NO_ATTRIBUTION_TOKENS,
    _PATTERN_METACHARS_RE,
    _PLACEMENT_ISSUE_CODES,
    _literal_directory,
    _plain_directory,
    write_prefix_allows,
)
from ai_flywheel.adapters.wiki.schema import (
    DEPOSIT_ROUTING_KEY,
    FORBIDDEN_PATH_SEGMENTS,
    _mapping,
    deposit_routing_rules,
    normalize_relative_path,
)
from ai_flywheel.adapters.wiki.t5_adapter import INCIDENT_SECTIONS, build_incident_card
from ai_flywheel.config import KnowledgeSinkConfig
from ai_flywheel.core.markdown_utils import parse_frontmatter, parse_frontmatter_file
from ai_flywheel.core.privacy import find_secret_shape
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import (
    INCIDENT_DIRECTORY_MARKER,
    INCIDENT_PAGE_TYPE,
    declares_private_page,
)


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


def discover_domain_pages(domain_dir: Path, *, limit: int = DOMAIN_INVENTORY_LIMIT) -> list[str]:
    """Basenames of the pages a project already holds in one domain directory.

    Direct children only: hidden files, archived pages and nested folders are not pages
    of the domain. Sorted by name and capped at ``limit``, which is all the planning
    prompt needs.
    """
    try:
        if not domain_dir.is_dir():
            return []
        names = sorted(
            (
                page.stem
                for page in domain_dir.iterdir()
                if page.is_file()
                and page.suffix.lower() == ".md"
                and not page.name.startswith(".")
                and not page.name.startswith("archive-")
            ),
            key=str.lower,
        )
    except OSError:
        return []
    return names[:limit]


def discover_practice_projects(projects_root: Path, *, domain_subdirs: Any = ()) -> list[dict[str, Any]]:
    """Dynamically inspect mounted Wiki project dirs that host a 实战笔记/ folder (zero hardcoded names).

    Each candidate carries the identifiers and the one-line description declared on its
    practice index page, so a planner can name it exactly and judge whether a note is about it.
    With ``domain_subdirs`` (the directories a Wiki's deposit routing declares) each candidate
    also carries its read-only ``domain_inventory``: the domain pages it already holds.
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
            fm = parse_frontmatter_file(index_file)
            if fm:
                if fm.get("project_id"):
                    candidate["project_id"] = str(fm["project_id"]).strip()
                candidate["aliases"] = _index_aliases(fm.get("aliases"))
                candidate["description"] = " ".join(str(fm.get("description") or "").split())
        if domain_subdirs:
            candidate["domain_inventory"] = {subdir: discover_domain_pages(child / subdir) for subdir in domain_subdirs}
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
    action: str = "create",
    source_sessions: list[str] | None = None,
) -> RouteDecision:
    """Decide, without any model call, whether a knowledge document is written and where.

    The planner's declaration (``wiki_route``) chooses the page type and the project; workspace
    and draft evidence only validate it. Without a declaration only the dry-run planner's
    evidence-based selection applies, and only when ``allow_evidence_fallback`` is set.

    ``action`` matters for a project-domain page only: there an ``update`` is merged into
    the page that exists and a ``create`` makes a new one, recording ``source_sessions``
    as its provenance. An incident card is always created, and records them the same way.
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

    check = context.validate_document(rel, content) if rel else None

    # A page of the incident ledger is judged by its own rules, before any other: it belongs
    # to no project, and no evidence may pick it. The branch is taken when the contract
    # declares the ledger and the plan names it — by its declaration, or by the page type
    # the draft itself states.
    incident = candidates.incident
    if incident is not None and (
        (has_declaration and declared["page_type"] == INCIDENT_PAGE_TYPE)
        or (not has_declaration and _frontmatter_field(content, "type").strip().lower() == INCIDENT_PAGE_TYPE)
    ):
        return _evaluate_incident_route(
            context=context,
            incident=incident,
            declared=declared,
            has_declaration=has_declaration,
            rel=rel,
            title=title,
            summary=summary,
            content=content,
            action=action,
            verdict=verdict,
            source_sessions=source_sessions or [],
        )

    # 0. A project-domain page follows the Wiki's deposit contract: the same project checks as a
    #    card, then the page's own (the domain's directory, an update merged, a create built).
    deposit = candidates.deposit_routing
    if deposit is not None:
        domain_type = declared["page_type"] if has_declaration else (check.page_type if check and check.ok else "")
        domains = deposit.domains_for(domain_type)
        if domains:
            return _evaluate_deposit_route(
                context=context,
                domains=domains,
                page_type=domain_type,
                declared=declared,
                has_declaration=has_declaration,
                allow_evidence_fallback=allow_evidence_fallback,
                rel=rel,
                title=title,
                summary=summary,
                content=content,
                action=action,
                check=check,
                workspaces=workspaces,
                haystack=haystack,
                workspace_hits=workspace_hits,
                content_hits=content_hits,
                verdict=verdict,
                source_sessions=source_sessions or [],
            )

    # 1. A draft that already satisfies the schema at its own path: the path decides.
    if check is not None:
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
        if not candidates.fallback_page_types:
            # Only the incident ledger is offered, and evidence never picks it.
            return verdict(False, code="no_routable_target")
        chosen = select_practice_project(
            candidates.projects,
            haystack,
            workspaces,
            fallback_project=context.sink.practice_fallback_project,
        )
        if chosen is None:
            return verdict(False, code="no_evidence")
        return verdict(True, source=ROUTE_EVIDENCE, page_type=candidates.fallback_page_types[0], project=chosen)

    # 4. A declaration is validated, never corrected.
    if declared["page_type"] == INCIDENT_PAGE_TYPE:
        # Reached only when the contract declares no ledger the engine can locate, so the
        # planner was never offered this page type: say that, not that the plan declared no project.
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的页面类型 {INCIDENT_PAGE_TYPE} 不在可路由类型内（挂载 Wiki 未声明可定位的事故台账）",
        )
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


def _domain_path_parts(rel: str, projects_root_rel: str) -> tuple[str, str, str] | None:
    """(project directory, domain directory, file name) of a page under the projects root.

    None when the path is not ``<projects root>/<project>/<domain directory>/<file>``.
    """
    root = projects_root_rel.strip("/")
    if not root or not rel.lower().startswith(root.lower() + "/"):
        return None
    parts = [segment for segment in rel[len(root) + 1 :].split("/") if segment]
    if len(parts) < 3:
        return None
    return parts[0], "/".join(parts[1:-1]), parts[-1]


def _frontmatter_field(content: str, key: str) -> str:
    """One frontmatter value of a page or a draft, or '' when it does not carry it."""
    fm, _ = parse_frontmatter(content or "")
    return str(fm.get(key) or "").strip()


def _issue_text(issues: list[Any]) -> str:
    return "；".join(f"{issue.get('code')}: {issue.get('message')}" for issue in issues[:5])


def _domain_template(domain: dict[str, Any], content: str) -> tuple[dict[str, Any] | None, str]:
    """The domain's page template a create follows, or a problem that says why not.

    A domain may hold several templates (an algorithm page and a caliber page under one
    page type); the draft's ``knowledge_kind`` says which. Without one, a single-template
    domain is unambiguous and a several-template domain is not: nothing is guessed.
    """
    templates = domain["templates"]
    by_kind = {template["knowledge_kind"]: template for template in templates if template.get("knowledge_kind")}
    if not by_kind:
        return (templates[0], "") if templates else (None, f"领域 {domain['domain']} 没有可用模板")
    kind = _frontmatter_field(content, "knowledge_kind")
    if kind in by_kind:
        return by_kind[kind], ""
    if not kind and len(by_kind) == 1:
        return next(iter(by_kind.values())), ""
    if kind:
        return None, f"草稿的 knowledge_kind「{kind}」不属于领域 {domain['domain']}（可为：{'、'.join(by_kind)}）"
    return None, f"领域 {domain['domain']} 有多个模板（{'、'.join(by_kind)}），草稿须声明 knowledge_kind"


def _incident_file_name_problem(file_name: str, today: str) -> str:
    """Why the file name of a ledger page is not a legal one, or '' when it is.

    Legal is ``<YYYY-MM-DD>-<slug>.md``, with a sequence number and a dash allowed in front:
    a real day no later than today, and a slug that names a page.
    """
    text = UNROUTED_REASON_TEXT["incident_file_name_invalid"]
    match = INCIDENT_FILE_NAME_RE.fullmatch(file_name or "")
    if not match:
        return f"{text}：{file_name or '（空）'}"
    try:
        day = date.fromisoformat(match.group(2))
    except ValueError:
        return f"{text}：{file_name} 的日期不是真实日期"
    if day > date.fromisoformat(today):
        return f"{text}：{file_name} 的日期晚于今天"
    slug = match.group(3).strip()
    if not slug or slug.startswith(".") or slug.lower().startswith("archive-"):
        return f"{text}：{file_name}"
    return ""


def _evaluate_incident_route(
    *,
    context: RouteContext,
    incident: IncidentRouting,
    declared: dict[str, str],
    has_declaration: bool,
    rel: str,
    title: str,
    summary: str,
    content: str,
    action: str,
    verdict: Callable[..., RouteDecision],
    source_sessions: list[str] | None = None,
) -> RouteDecision:
    """Route a page of the Wiki's incident ledger. Every check refuses; none of them corrects the plan.

    The ledger is closed unless the sink is allowed to write into it, and a card enters it
    by a declaration alone. The path the planner named is validated as it stands — inside
    the ledger, named after a day, not already a page — and the card is then the draft's
    own text under the standard section frame with the metadata the engine can know. It is
    always a sensitive write: under the ``smart`` and ``always`` approval modes it waits for a
    human like any other sensitive page, and whether ``auto`` does is the approval contract's.
    """
    candidates = context.candidates
    ledger = incident.directory
    # 1. The second lock: the sink's write prefixes must open the ledger. Read from the
    #    configuration as it is now, not as it was when the candidates were discovered.
    if not write_prefix_allows(ledger, context.sink.allowed_write_prefixes):
        return verdict(
            False,
            code="incident_prefix_not_allowed",
            detail=f"{UNROUTED_REASON_TEXT['incident_prefix_not_allowed']}：{ledger}/",
        )
    # 2. By declaration alone: no evidence ever picks the ledger.
    if not has_declaration:
        return verdict(
            False,
            code="declaration_missing",
            detail=f"{UNROUTED_REASON_TEXT['declaration_missing']}：事故卡只能经路由声明进入台账",
        )
    # The project is optional on an incident card; a declared one is validated, never corrected.
    state, project = declared_project(declared["project"], candidates.projects)
    if state == "invalid":
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的项目 {declared['project'] or '（空）'} 不能唯一匹配候选项目",
        )
    if action != "create":
        return verdict(
            False, code="declaration_invalid", detail=f"事故卡只支持新建（create），计划动作为「{action or '空'}」"
        )
    draft_type = _frontmatter_field(content, "type")
    if draft_type and draft_type != INCIDENT_PAGE_TYPE:
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的页面类型「{INCIDENT_PAGE_TYPE}」与草稿页面的类型「{draft_type}」不一致",
        )

    # 3. The path names one page directly inside the ledger.
    parent, _, file_name = rel.rpartition("/")
    if parent != ledger:
        return verdict(
            False,
            code="incident_path_mismatch",
            detail=f"{UNROUTED_REASON_TEXT['incident_path_mismatch']}：目标路径须为 "
            f"{ledger}/<YYYY-MM-DD>-<名称>.md，实际为 {rel or '（空）'}",
        )
    today = shanghai_today_ymd()
    problem = _incident_file_name_problem(file_name, today)
    if problem:
        return verdict(False, code="incident_file_name_invalid", detail=problem)
    if context.read_document is not None and context.read_document(rel) is not None:
        # An incident card is a record of one event: an existing one is never written over.
        return verdict(False, code="incident_target_exists", detail=f"{UNROUTED_REASON_TEXT['incident_target_exists']}：{rel}")

    # 4. The ledger is sensitive, so its pages are read by the strict privacy check.
    shape = find_secret_shape(content, strict=True)
    if shape:
        return verdict(
            False,
            code="secret_leak_blocked",
            detail=f"{SECRET_LEAK_BLOCKED}：事故卡草稿命中密码、Token 或私钥明文形态（{shape}），密钥门禁阻断",
        )

    # 5. The card, then the contract's own verdict on it.
    page, problems = build_incident_card(
        title=title,
        summary=summary,
        content=content,
        required_fields=incident.required_fields,
        allowed_status=incident.allowed_status,
        load_policy=incident.load_policy,
        today=today,
        source_sessions=list(source_sessions or []),
    )
    gate_issues = [{"code": "INCIDENT_PAGE_BUILD", "message": note} for note in problems]
    if not gate_issues:
        gate = context.validate_document(rel, page)
        gate_issues = list(gate.issues) if not gate.ok else []
    if gate_issues:
        placement = [issue for issue in gate_issues if issue.get("code") in _PLACEMENT_ISSUE_CODES]
        code = "path_pattern_mismatch" if placement else "incident_card_invalid"
        return verdict(False, code=code, detail=f"{UNROUTED_REASON_TEXT[code]}：{_issue_text(placement or gate_issues)}")
    decision = verdict(True, source=ROUTE_DECLARED, page_type=INCIDENT_PAGE_TYPE, project=project)
    decision.sensitive = True
    decision.page = page
    return decision


def _evaluate_deposit_route(
    *,
    context: RouteContext,
    domains: list[dict[str, Any]],
    page_type: str,
    declared: dict[str, str],
    has_declaration: bool,
    allow_evidence_fallback: bool,
    rel: str,
    title: str,
    summary: str,
    content: str,
    action: str,
    check: Any,
    workspaces: list[str],
    haystack: str,
    workspace_hits: list[str],
    content_hits: list[str],
    verdict: Callable[..., RouteDecision],
    source_sessions: list[str] | None = None,
) -> RouteDecision:
    """Route a project-domain page: the card's project checks first, then the page's own.

    The declaration names the page type and the project; the path names one page inside
    the domain's directory. An ``update`` is merged into the page that exists and never
    replaces it. A ``create`` that meets an existing page becomes an update of it, so an
    existing page is never overwritten without the merge showing what it adds.
    """
    candidates = context.candidates
    if not candidates.routable:
        return verdict(False, code="no_routable_target")
    if action not in ("create", "update"):
        return verdict(
            False, code="declaration_invalid", detail=f"领域页只支持 create 或 update，计划动作为「{action or '空'}」"
        )
    if has_declaration:
        state, project = declared_project(declared["project"], candidates.projects)
        if state == "none":
            return verdict(False, code="declared_none")
        if project is None:
            return verdict(
                False,
                code="declaration_invalid",
                detail=f"声明的项目 {declared['project'] or '（空）'} 不能唯一匹配候选项目",
            )
    else:
        if not allow_evidence_fallback:
            return verdict(False, code="declaration_missing")
        project = project_of_path(rel, candidates.projects, context.projects_root_rel)
        if project is None:
            return verdict(False, code="no_routable_target")
        chosen = select_practice_project(
            candidates.projects, haystack, workspaces, fallback_project=context.sink.practice_fallback_project
        )
        if chosen is None:
            return verdict(False, code="no_evidence")
        if chosen is not project:
            return verdict(
                False,
                code="path_mismatch",
                detail=f"证据指向 {chosen['dir_name']}，目标路径所示项目为 {project['dir_name']}",
            )
    name = project["dir_name"]
    conflict = _declaration_conflict(name, workspace_hits, content_hits)
    if conflict:
        return verdict(False, code=conflict[0], detail=conflict[1])

    # 1. The path names one page inside the domain's directory.
    parts = _domain_path_parts(rel, context.projects_root_rel)
    domain = next((item for item in domains if parts and item["subdir"] == parts[1]), None)
    if parts is None or parts[0].lower() != name.lower() or domain is None:
        where = " | ".join(item["subdir"] for item in domains)
        return verdict(
            False,
            code="domain_path_mismatch",
            detail=f"{UNROUTED_REASON_TEXT['domain_path_mismatch']}：目标路径须为 "
            f"{context.projects_root_rel}/{name}/({where})/<页面名>.md，实际为 {rel or '（空）'}",
        )
    file_name = parts[2]
    if not file_name.lower().endswith(".md") or any(
        segment.startswith((".", "archive-")) or segment in FORBIDDEN_PATH_SEGMENTS for segment in rel.split("/")
    ):
        return verdict(
            False,
            code="domain_path_mismatch",
            detail=f"{UNROUTED_REASON_TEXT['domain_path_mismatch']}：{rel} 不是领域目录下的 Markdown 页面",
        )
    stem = file_name[: -len(".md")]
    if any(template.get("knowledge_kind") == "ddl" for template in domain["templates"]) and not is_single_table_name(stem):
        return verdict(
            False,
            code="table_page_name_invalid",
            detail=f"{UNROUTED_REASON_TEXT['table_page_name_invalid']}：{stem}",
        )

    # 2. The page that exists, if any, decides what the action can mean.
    existing = context.read_document(rel) if context.read_document is not None else None
    flags: dict[str, Any] = {
        "domain": domain["domain"],
        "sensitive": bool(domain["sensitive"]) or declares_private_page(existing or content),
    }
    if flags["sensitive"]:
        # A configuration page records where a credential is kept, never the credential itself.
        # Only what would enter the page is judged, and the trace names the shape, never the value.
        shape = find_secret_shape(content, strict=True)
        if shape:
            return verdict(
                False,
                code="secret_leak_blocked",
                detail=f"{SECRET_LEAK_BLOCKED}：{UNROUTED_REASON_TEXT['secret_leak_blocked']}（{shape}）",
            )

    def routed(**extra: Any) -> RouteDecision:
        decision = verdict(True, source=ROUTE_DECLARED if has_declaration else ROUTE_PATH, page_type=page_type, project=project)
        for key, value in {**flags, **extra}.items():
            setattr(decision, key, value)
        return decision

    if action == "update" or existing is not None:
        if existing is None:
            return verdict(
                False, code="update_target_missing", detail=f"{UNROUTED_REASON_TEXT['update_target_missing']}：{rel}"
            )
        existing_type = _frontmatter_field(existing, "type")
        if existing_type != page_type:
            return verdict(
                False,
                code="update_target_type_mismatch",
                detail=f"{UNROUTED_REASON_TEXT['update_target_type_mismatch']}：{rel} 为「{existing_type or '未知'}」，"
                f"路由声明为「{page_type}」",
            )
        template_sections = [name for template in domain["templates"] for name in template["body_sections"]]
        merge = merge_domain_increment(existing, content, template_sections=template_sections)
        if not merge.ok:
            return verdict(
                False,
                code="increment_invalid",
                detail=f"{UNROUTED_REASON_TEXT['increment_invalid']}：{'；'.join(merge.problems)}",
            )
        if not merge.has_additions:
            return verdict(
                False,
                code="update_no_new_content",
                detail=f"{UPDATE_NO_NEW_CONTENT}：{UNROUTED_REASON_TEXT['update_no_new_content']}"
                f"（{stem}，{merge.duplicates} 条均已存在）",
            )
        gate = context.validate_document(rel, merge.merged)
        if not gate.ok:
            return verdict(
                False,
                code="update_target_invalid",
                detail=f"{UNROUTED_REASON_TEXT['update_target_invalid']}：{_issue_text(gate.issues)}",
            )
        return routed(
            incremental=True,
            increment=merge.added_text,
            duplicates=merge.duplicates,
            placeholders=list(merge.placeholders),
            relabel=CREATE_TARGET_EXISTS if action == "create" else "",
        )

    # 3. A new page: the draft when it already satisfies the schema, else the template's shape.
    if check is not None and check.ok:
        if check.page_type != page_type:
            return verdict(
                False,
                code="declaration_invalid",
                detail=f"声明的页面类型「{page_type}」与草稿页面的类型「{check.page_type}」不一致",
            )
        return routed(page=content)
    draft_type = _frontmatter_field(content, "type")
    if draft_type and draft_type != page_type:
        # The same refusal for a draft the engine would rebuild: one that states another page
        # type than the declaration is contradicted, not corrected.
        return verdict(
            False,
            code="declaration_invalid",
            detail=f"声明的页面类型「{page_type}」与草稿页面的类型「{draft_type}」不一致",
        )
    template, problem = _domain_template(domain, content)
    if template is None:
        return verdict(False, code="declaration_invalid", detail=problem)
    page, problems = build_domain_page(
        title=title,
        summary=summary,
        content=content,
        page_type=page_type,
        project_id=str(project.get("project_id") or name),
        template=template,
        today=shanghai_today_ymd(),
        source_sessions=list(source_sessions or []),
    )
    gate_issues = [{"code": "DOMAIN_PAGE_BUILD", "message": note} for note in problems]
    if not gate_issues:
        gate = context.validate_document(rel, page)
        gate_issues = list(gate.issues) if not gate.ok else []
    if gate_issues:
        placement = [issue for issue in gate_issues if issue.get("code") in _PLACEMENT_ISSUE_CODES]
        code = "path_pattern_mismatch" if placement else "domain_page_invalid"
        return verdict(False, code=code, detail=f"{UNROUTED_REASON_TEXT[code]}：{_issue_text(placement or gate_issues)}")
    return routed(page=page)
