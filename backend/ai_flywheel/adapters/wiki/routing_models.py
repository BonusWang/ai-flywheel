"""Routing data models, reason vocabularies, and contract schema projections.

Extracted from :mod:`ai_flywheel.adapters.wiki.router` so the data contracts
(``IncidentRouting``, ``DepositRouting``, ``RoutingCandidates``,
``RouteDecision``, ``RouteContext``) evolve independently of the evidence and
verdict evaluation logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_flywheel.adapters.wiki.schema import (
    DEPOSIT_ROUTING_KEY,
    FORBIDDEN_PATH_SEGMENTS,
    _mapping,
    deposit_routing_rules,
)
from ai_flywheel.adapters.wiki.t5_adapter import INCIDENT_SECTIONS
from ai_flywheel.config import KnowledgeSinkConfig
from ai_flywheel.models import INCIDENT_DIRECTORY_MARKER, INCIDENT_PAGE_TYPE

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
    # A project-domain page the deposit contract declares (content_contract.deposit_routing).
    "domain_path_mismatch": "目标路径不在声明页面类型的领域落点目录内",
    "path_pattern_mismatch": "目标路径与该页面类型声明的分区或 path_pattern 不匹配",
    "table_page_name_invalid": "一表一页：表结构页的文件名只能是单张表名",
    "update_target_missing": "update 的目标领域页不存在（缺页应使用 create）",
    "update_target_type_mismatch": "update 的目标页页面类型与路由声明不一致",
    "update_target_invalid": "目标页未通过挂载 Wiki 的写前门禁，不能在其上合并增量",
    "update_no_new_content": "增量内容均已存在于目标页，没有新增实质内容",
    "increment_invalid": "增量内容无法无损并入目标页",
    "domain_page_invalid": "按模板生成的领域页未通过挂载 Wiki 的写前门禁",
    "secret_leak_blocked": "增量内容命中密码、Token 或私钥明文形态，密钥门禁阻断",
    # A page of the Wiki's incident ledger. The ledger is closed until the sink is allowed
    # to write into it, and a card enters it by a declaration alone.
    "incident_prefix_not_allowed": "事故台账目录未在 knowledge_sink.allowed_write_prefixes 中放行",
    "incident_path_mismatch": "目标路径不在挂载 Wiki 声明的事故台账目录内",
    "incident_file_name_invalid": "事故卡文件名须为 <YYYY-MM-DD>-<名称>.md（可带序号前缀）且日期不晚于今天",
    "incident_card_invalid": "按标准章节框架整理的事故卡未通过挂载 Wiki 的写前门禁",
    "incident_target_exists": "事故卡只支持新建，目标页面已存在",
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
    "domain_path_mismatch": "领域落点路径不符",
    "path_pattern_mismatch": "路径模式不符",
    "table_page_name_invalid": "表页命名不符",
    "update_target_missing": "更新目标不存在",
    "update_target_type_mismatch": "更新目标类型不符",
    "update_target_invalid": "更新目标未过门禁",
    "update_no_new_content": "增量无新增内容",
    "increment_invalid": "增量格式不符",
    "domain_page_invalid": "领域页未过门禁",
    "secret_leak_blocked": "密钥门禁阻断",
    "incident_prefix_not_allowed": "事故前缀未放行",
    "incident_path_mismatch": "事故卡路径不符",
    "incident_file_name_invalid": "事故卡文件名不符",
    "incident_card_invalid": "事故卡未过门禁",
    "incident_target_exists": "事故卡目标已存在",
}
# How many existing page names per domain a candidate project carries into the planning prompt.
DOMAIN_INVENTORY_LIMIT = 100
# The trace marker of an update whose content the page already holds (the Skill normaliser's spelling).
UPDATE_NO_NEW_CONTENT = "UPDATE_NO_NEW_CONTENT"
# The trace marker of a planned item the privacy gate refused before it could reach a page.
SECRET_LEAK_BLOCKED = "SECRET_LEAK_BLOCKED"
# The trace marker of a create that met an existing page and was merged into it instead.
CREATE_TARGET_EXISTS = "CREATE_TARGET_EXISTS"
# Gate issues that are about where a page sits rather than what it says.
_PLACEMENT_ISSUE_CODES = {
    "CORE_FILE_BLOCKED",
    "FORBIDDEN_PATH",
    "PARTITION_UNKNOWN",
    "PLACEMENT_OUTSIDE_PARTITION",
    "PATH_PATTERN_MISMATCH",
    "FILE_PATTERN_MISMATCH",
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

# The name of a page of the incident ledger: the day, a slug, and a sequence number the
# Wiki may put in front of the day.
INCIDENT_FILE_NAME_RE = re.compile(r"^(?:(\d{1,4})-)?(\d{4}-\d{2}-\d{2})-([^/\\]+)\.md$")
# The first character of a `path_pattern` that stops a run of plain letters.
_PATTERN_METACHARS_RE = re.compile(r"[\\.()\[\]{}*+?|^$]")


def _plain_directory(name: str) -> bool:
    """Whether ``name`` is a plain directory path: real segments, none of them excluded."""
    segments = [segment for segment in name.split("/") if segment]
    return bool(segments) and all(
        segment not in {".", ".."} and segment == segment.strip()
        and segment not in FORBIDDEN_PATH_SEGMENTS
        and not segment.startswith("archive-")
        for segment in segments
    )


def write_prefix_allows(directory: str, prefixes: list[str]) -> bool:
    """Whether ``knowledge_sink.allowed_write_prefixes`` lets the sink write into ``directory``.

    The writer's own rule: a path may be written when it starts with one of the prefixes,
    so a directory may be written into when the pages under it would pass. An empty list
    restrains nothing, and therefore opens nothing by name: for a directory that is closed
    until it is opened explicitly, that is not enough.
    """
    allowed = [str(prefix).replace("\\", "/").lstrip("/") for prefix in (prefixes or [])]
    target = f"{str(directory).strip('/')}/"
    return bool(allowed) and any(target.startswith(prefix) for prefix in allowed)


def _literal_directory(path_pattern: Any) -> str:
    """The directory a ``path_pattern`` names in plain letters ahead of its first wildcard.

    ``^ai-incidents/[^/]+\\.md$`` names ``ai-incidents``, and so does
    ``^ai-incidents/INC-[^/]+\\.md$``: a page's own name may begin in plain letters, and the
    directory is what stands before it. A pattern whose plain part holds no slash names none.
    """
    plain = _PATTERN_METACHARS_RE.split(str(path_pattern or "").lstrip("^"), maxsplit=1)[0]
    directory = plain[: plain.rfind("/")] if "/" in plain else ""
    return directory if _plain_directory(directory) else ""


@dataclass
class IncidentRouting:
    """The Wiki's incident ledger as its contract declares it, resolved for routing and planning.

    Read off ``content_contract.page_types.incident`` and the partition it names: the
    directory the ledger is kept under, and what the contract requires of such a page.
    Nothing here is hardcoded: a Wiki that keeps its ledger elsewhere gets that directory.

    ``writable`` records whether ``knowledge_sink.allowed_write_prefixes`` lets the sink
    write into the ledger. It is the second of two locks: the contract declares the page
    type, the configuration opens the directory, and only both together offer the ledger.
    """

    directory: str
    required_fields: list[str] = field(default_factory=list)
    allowed_status: list[str] = field(default_factory=list)
    load_policy: str = ""
    writable: bool = False

    @classmethod
    def from_schema(
        cls, schema: dict[str, Any], sink: KnowledgeSinkConfig | None = None
    ) -> IncidentRouting | None:
        """The ledger a contract declares, or None when it declares none that can be located.

        The directory is the one the page type's ``path_pattern`` names in plain letters;
        else the partition sub-directory whose name starts with the incident marker, among
        the sub-directories the partition declares; else the partition root itself. A
        partition that declares sub-directories and none of that name locates no ledger.
        """
        content = _mapping(schema.get("content_contract"))
        type_cfg = _mapping(_mapping(content.get("page_types")).get(INCIDENT_PAGE_TYPE))
        if not type_cfg:
            return None
        partition_id = str(type_cfg.get("partition") or "")
        partition = _mapping(_mapping(_mapping(schema.get("partition_contract")).get("partitions")).get(partition_id))
        root = str(partition.get("root") or "").replace("\\", "/").strip("/")
        if not root:
            return None
        subdirs = [
            name
            for name in (
                str(raw).replace("\\", "/").strip("/") for raw in (partition.get("allowed_subdirectories") or []) if isinstance(raw, str)
            )
            if name
        ]
        literal = _literal_directory(type_cfg.get("path_pattern"))
        if literal:
            directory = f"{root}/{literal}"
        elif not subdirs:
            directory = root
        else:
            named = next(
                (name for name in subdirs if name.split("/")[0].lower().startswith(INCIDENT_DIRECTORY_MARKER) and _plain_directory(name)),
                "",
            )
            if not named:
                return None
            directory = f"{root}/{named}"
        core = [str(name) for name in (_mapping(schema.get("core_contract")).get("required_fields") or [])]
        own = [str(name) for name in (type_cfg.get("required_fields") or [])]
        ledger = cls(
            directory=directory,
            required_fields=list(dict.fromkeys([*core, *own])),
            allowed_status=[str(value) for value in (type_cfg.get("allowed_status") or [])],
            load_policy=str(type_cfg.get("load_policy") or "").strip(),
        )
        ledger.writable = sink is not None and write_prefix_allows(directory, sink.allowed_write_prefixes)
        return ledger

    def to_dict(self) -> dict[str, Any]:
        """The form the planning prompt carries."""
        return {
            "page_type": INCIDENT_PAGE_TYPE,
            "directory": f"{self.directory}/",
            "file_name": "<YYYY-MM-DD>-<slug>.md",
            "sections": list(INCIDENT_SECTIONS),
        }


@dataclass
class DepositRouting:
    """The Wiki's declared project-domain deposit routing, resolved for routing and planning.

    Read off ``content_contract.deposit_routing``: which domain each trigger feeds, the
    directory under a project that holds its pages, and the page templates they follow.
    Nothing here is hardcoded: a Wiki that declares other domains gets those.
    """

    version: str
    rule: str
    domains: list[dict[str, Any]]
    required_fields: dict[str, list[str]] = field(default_factory=dict)  # frontmatter a page type requires
    # Where the Wiki keeps its project directories, so a planner can write a page's full path.
    projects_root: str = ""

    @classmethod
    def from_schema(cls, schema: dict[str, Any]) -> DepositRouting | None:
        """The routing a contract declares, or None when it declares none."""
        rules = deposit_routing_rules(schema)
        if not rules:
            return None
        content = _mapping(schema.get("content_contract"))
        block = _mapping(content.get(DEPOSIT_ROUTING_KEY))
        declared = _mapping(content.get("page_types"))
        core = [str(name) for name in (_mapping(schema.get("core_contract")).get("required_fields") or [])]
        required: dict[str, list[str]] = {}
        for rule in rules:
            for page_type in rule["page_types"]:
                type_cfg = _mapping(declared.get(page_type))
                # The core fields first, then the page type's own; a field both name is listed once.
                required[page_type] = list(dict.fromkeys([*core, *(str(name) for name in (type_cfg.get("required_fields") or []))]))
        return cls(
            version=str(block.get("version") or ""),
            rule=" ".join(str(block.get("rule") or "").split()),
            domains=rules,
            required_fields=required,
        )

    @property
    def page_types(self) -> list[str]:
        """Every page type the declared domains route to."""
        return list(dict.fromkeys(page_type for domain in self.domains for page_type in domain["page_types"]))

    @property
    def subdirs(self) -> list[str]:
        """The directory under a project that holds each domain's pages."""
        return [domain["subdir"] for domain in self.domains]

    def domains_for(self, page_type: str) -> list[dict[str, Any]]:
        """The declared domains a page type belongs to (several when they share the type)."""
        return [domain for domain in self.domains if page_type in domain["page_types"]]

    def sensitive_subdirs(self) -> list[str]:
        """Directories whose pages are a sensitive write: the project's configuration."""
        return [domain["subdir"] for domain in self.domains if domain["sensitive"]]

    def page_path(self, subdir: str, project: str, page: str) -> str:
        """The relative path of one domain page: ``<projects root>/<project>/<domain directory>/<page>.md``."""
        return "/".join(part for part in (self.projects_root, project, subdir, f"{page}.md") if part)

    def to_dict(self) -> dict[str, Any]:
        """The form the planning prompt carries."""
        return {
            "version": self.version,
            "rule": self.rule,
            "projects_root": self.projects_root,
            "domains": [
                {
                    "domain": domain["domain"],
                    "trigger": domain["trigger"],
                    "target": domain["target"],
                    "target_path": self.page_path(domain["subdir"], "{project}", "{page}"),
                    "page_types": list(domain["page_types"]),
                    "required_fields": {
                        page_type: list(self.required_fields.get(page_type, [])) for page_type in domain["page_types"]
                    },
                    "templates": [dict(template) for template in domain["templates"]],
                    "sensitive": domain["sensitive"],
                }
                for domain in self.domains
            ],
        }


@dataclass
class RoutingCandidates:
    """Page types and projects a knowledge document may be routed to, read off the mounted Wiki."""

    page_types: list[str] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    # Page types the evidence fallback may pick without a declaration (the dry-run planner
    # only): the practice card types. A project-domain page type is reached by declaration alone.
    evidence_page_types: list[str] = field(default_factory=list)
    # The Wiki's declared project-domain deposit routing; None when the contract declares none.
    deposit_routing: DepositRouting | None = None
    # The Wiki's incident ledger; None when the contract declares no incident page type it
    # can locate. It is offered only while `writable` says the sink may write into it.
    incident: IncidentRouting | None = None

    @property
    def incident_offered(self) -> bool:
        """Whether the planner may plan an incident card: the contract declares the ledger
        and the sink's write prefixes open it. Closed by default."""
        return self.incident is not None and self.incident.writable

    @property
    def routable(self) -> bool:
        return bool(self.page_types and self.projects) or self.incident_offered

    @property
    def fallback_page_types(self) -> list[str]:
        """What the evidence fallback may choose: the evidence page types, else every page type.

        An incident page is never among them: the ledger is reached by a declaration alone.
        """
        types = self.evidence_page_types or self.page_types
        return [page_type for page_type in types if page_type != INCIDENT_PAGE_TYPE]

    def to_dict(self, *, description_limit: int = 0) -> dict[str, Any]:
        """Serialisable form; ``description_limit`` caps each project description (0 keeps it whole).

        The deposit routing, each project's domain inventory and the incident ledger appear
        only when the Wiki declares them: a Wiki that does not keeps exactly the prompt it had.
        """
        payload: dict[str, Any] = {
            "page_types": list(self.page_types),
            "projects": [],
        }
        for project in self.projects:
            entry: dict[str, Any] = {
                "project": project["dir_name"],
                "project_id": project["project_id"],
                "aliases": list(project.get("aliases") or []),
                "description": (project.get("description") or "")[:description_limit]
                if description_limit
                else project.get("description") or "",
            }
            for key in ("domain_inventory", "domain_page_sections"):
                if project.get(key):
                    entry[key] = project[key]
            payload["projects"].append(entry)
        if self.deposit_routing is not None:
            payload["deposit_routing"] = self.deposit_routing.to_dict()
        if self.incident is not None and self.incident_offered:
            payload["incident_pages"] = self.incident.to_dict()
        return payload


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
    # A project-domain page (content_contract.deposit_routing): the domain it was routed to,
    # whether the write merges an increment into the existing page, and whether it is a sensitive
    # write (the project's configuration page, or a page that declares itself private). An
    # incident card is sensitive too, by the kind of page it is.
    domain: str = ""
    incremental: bool = False
    sensitive: bool = False
    relabel: str = ""  # CREATE_TARGET_EXISTS when a create met an existing page and became an update
    # Working state the normalisation step reads back: the page a create will write, or the
    # increment an update will merge (already free of what the page holds).
    page: str = ""
    increment: str = ""
    duplicates: int = 0
    placeholders: list[str] = field(default_factory=list)

    @property
    def project_dir(self) -> str:
        return str(self.project["dir_name"]) if self.project else ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.domain:
            # `duplicates`: rows and blocks of the increment the page already held. They were
            # dropped before anything was written, and this is where that is recorded.
            payload.update(
                domain=self.domain,
                incremental=self.incremental,
                sensitive=self.sensitive,
                relabel=self.relabel,
                duplicates=self.duplicates,
            )
        elif self.sensitive:
            # Sensitive by the kind of page it is (an incident card), not by the domain it sits in.
            payload["sensitive"] = self.sensitive
        return payload


@dataclass
class RouteContext:
    """What the routing verdict needs from the gate around it."""

    schema: dict[str, Any]
    candidates: RoutingCandidates
    projects_root_rel: str
    sink: KnowledgeSinkConfig
    #: The gate's own document check, so a draft that already satisfies the schema keeps its path.
    validate_document: Callable[[str, str], Any]
    #: Reads a page the sink may write, or None when it does not exist. Only a domain-page
    #: update needs it: an increment is merged into the page as it is, never into a redrawn one.
    read_document: Callable[[str], str | None] | None = None
