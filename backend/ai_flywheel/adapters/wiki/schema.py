"""The mounted Wiki's schema contract.

A schema-governed Wiki declares itself in `.wiki-schema.md` at its root. This module
reads that contract (once per file revision), splits a document's frontmatter from its
body, and knows which path segments no P04 write may touch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.core.markdown_utils import split_frontmatter
from ai_flywheel.models import SENSITIVE_CONFIG_PAGE_TYPE

# The schema contract of a mounted Wiki: never read back through the document surface.
SCHEMA_FILE_NAME = ".wiki-schema.md"

# Path segments no P04 write may touch (raw material, archives, process memory).
FORBIDDEN_PATH_SEGMENTS = {"raw", "raw-原始素材", ".wiki-tmp", ".wiki-agent-memory"}

# The machine blocks a contract must declare to be usable by the gate.
REQUIRED_SCHEMA_BLOCKS = ("core_contract", "partition_contract", "content_contract")

# The partition that holds the Wiki's projects, and the root it is declared under.
PROJECTS_PARTITION_ID = "projects"
DEFAULT_PROJECTS_ROOT = "wiki-知识库/projects-项目"

# Where the content contract declares which project domain each kind of project knowledge
# flows into, and the catalogue its domains name their page templates from.
DEPOSIT_ROUTING_KEY = "deposit_routing"
PROJECT_TEMPLATE_CONTRACT_KEY = "project_template_contract"

# A page the Wiki loads only on an explicit request or a task's need holds material it does not
# want in every context (the project config page); writing into it is a sensitive write.
SENSITIVE_LOAD_POLICY = "explicit_or_task_required"

# The placeholder a deposit target starts with; what follows is the directory under the project.
_PROJECT_TARGET_PREFIX = "{project}/"


class WikiGateError(ValueError):
    """Raised when a P04 write is blocked by the schema-aware Wiki gate."""


def normalize_relative_path(relative_path: str) -> str:
    """A relative path in the Wiki's own notation: forward slashes, no leading ``./`` or ``/``."""
    rel = (relative_path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _mapping(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _section_names(raw: Any) -> list[str]:
    """Body section names a template declares, in template order.

    An entry is a name, or a mapping that carries it under ``name`` (or ``title``).
    """
    items = [raw] if isinstance(raw, str) else raw
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        name = item.get("name") or item.get("title") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _target_subdir(target: Any, domain: str, source: str) -> str:
    """The directory under a project that a deposit target names: ``{project}/<dir>/`` -> ``<dir>``."""
    text = str(target or "").strip().replace("\\", "/")
    segments = text[len(_PROJECT_TARGET_PREFIX) :].split("/") if text.startswith(_PROJECT_TARGET_PREFIX) else []
    segments = [segment for segment in segments if segment]
    if not segments or any(segment != segment.strip() or segment in {".", ".."} for segment in segments):
        raise WikiGateError(
            f"{source} {DEPOSIT_ROUTING_KEY} domain '{domain}' has an unusable target {text or '（空）'}: "
            "expected {project}/<directory>/"
        )
    return "/".join(segments)


def _template_catalogue(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The schema's page templates by id: a list of mappings, or a mapping keyed by id."""
    contract = _mapping(content.get(PROJECT_TEMPLATE_CONTRACT_KEY))
    raw = contract.get("templates")
    if isinstance(raw, dict):
        return {str(key): _mapping(value) for key, value in raw.items()}
    return {
        str(template.get("id")): template
        for template in (raw if isinstance(raw, list) else [])
        if isinstance(template, dict) and str(template.get("id") or "").strip()
    }


def _resolve_template(
    reference: str, domain: str, catalogue: dict[str, dict[str, Any]], declared_types: dict[str, Any], source: str
) -> dict[str, Any]:
    template = catalogue.get(reference)
    if template is None:
        if reference not in declared_types:
            raise WikiGateError(
                f"{source} {DEPOSIT_ROUTING_KEY} domain '{domain}' names template '{reference}', "
                "which the template contract does not declare"
            )
        template = {"id": reference}  # a bare page type used as its own template
    page_type = str(template.get("page_type") or reference).strip()
    type_cfg = _mapping(declared_types.get(page_type))
    if not type_cfg:
        raise WikiGateError(
            f"{source} {DEPOSIT_ROUTING_KEY} domain '{domain}' resolves template '{reference}' to "
            f"page type '{page_type}', which the schema does not declare"
        )
    return {
        "id": str(template.get("id") or reference),
        "name": str(template.get("name") or "").strip(),
        "page_type": page_type,
        "knowledge_kind": str(template.get("knowledge_kind") or "").strip(),
        "load_policy": str(template.get("load_policy") or type_cfg.get("load_policy") or "").strip(),
        "body_sections": _section_names(template.get("body_sections")),
    }


def deposit_routing_rules(schema: dict[str, Any], *, source: str = SCHEMA_FILE_NAME) -> list[dict[str, Any]]:
    """The Wiki's project-domain deposit routing, one rule per declared domain.

    ``content_contract.deposit_routing`` says which project domain each trigger feeds, which
    directory under a project holds that domain's pages, and which page template they follow.
    Nothing declared means nothing routed: the caller keeps its behaviour for a Wiki that
    does not declare the block.

    A declaration that cannot be honoured raises `WikiGateError` instead of being half-read:
    a domain that names a template the contract does not declare, or a page type the schema
    does not know, would otherwise quietly swallow every plan item meant for it.
    """
    content = _mapping(schema.get("content_contract"))
    block = content.get(DEPOSIT_ROUTING_KEY)
    if block is None or block == "" or block == {}:
        return []
    if not isinstance(block, dict):
        raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY} is not a mapping")
    version = str(block.get("version") or "").strip()
    if version and not version.startswith("1."):
        raise WikiGateError(
            f"{source} content_contract.{DEPOSIT_ROUTING_KEY} declares version '{version}'; "
            "this gate reads version 1.x"
        )
    domains = block.get("domains")
    if domains is None:
        return []
    if not isinstance(domains, list):
        raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY}.domains is not a list")

    declared_types = _mapping(content.get("page_types"))
    catalogue = _template_catalogue(content)
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in domains:
        if not isinstance(raw, dict):
            raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY}.domains entries must be mappings")
        domain = str(raw.get("domain") or "").strip()
        if not domain:
            raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY} has a domain without a name")
        subdir = _target_subdir(raw.get("target"), domain, source)
        if domain in seen:
            raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY} declares domain '{domain}' twice")
        seen.add(domain)
        references = [raw.get("template")] if isinstance(raw.get("template"), str) else raw.get("template")
        if not isinstance(references, list) or not references:
            raise WikiGateError(f"{source} content_contract.{DEPOSIT_ROUTING_KEY} domain '{domain}' names no template")
        templates = [
            _resolve_template(str(reference).strip(), domain, catalogue, declared_types, source)
            for reference in references
        ]
        partitions = _mapping(_mapping(schema.get("partition_contract")).get("partitions"))
        for page_type in {template["page_type"] for template in templates}:
            partition = _mapping(partitions.get(str(_mapping(declared_types.get(page_type)).get("partition") or "")))
            if not str(partition.get("root") or "").strip("/"):
                raise WikiGateError(
                    f"{source} {DEPOSIT_ROUTING_KEY} domain '{domain}' routes page type '{page_type}' "
                    "into a partition the schema does not resolve"
                )

        def distinct(key: str) -> list[str]:
            return list(dict.fromkeys(str(template[key]) for template in templates if template[key]))

        rules.append(
            {
                "domain": domain,
                "trigger": " ".join(str(raw.get("trigger") or "").split()),
                "target": f"{_PROJECT_TARGET_PREFIX}{subdir}/",
                "subdir": subdir,
                "templates": templates,
                "page_types": distinct("page_type"),
                "knowledge_kinds": distinct("knowledge_kind"),
                "load_policies": distinct("load_policy"),
                "sensitive": any(
                    template["page_type"] == SENSITIVE_CONFIG_PAGE_TYPE
                    or template["load_policy"] == SENSITIVE_LOAD_POLICY
                    for template in templates
                ),
            }
        )
    return rules


class WikiSchemaLoader:
    """Reads the mounted Wiki's `.wiki-schema.md`, cached by the file's revision.

    The contract is read once per file revision: a batch that validates ten documents
    parses the schema once, and a contract edited between two runs is picked up.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: tuple[float, dict[str, Any]] | None = None

    @property
    def schema_path(self) -> Path:
        return self.root / SCHEMA_FILE_NAME

    def load(self, required_blocks: tuple[str, ...] = REQUIRED_SCHEMA_BLOCKS) -> dict[str, Any]:
        """The parsed contract. Raises `WikiGateError` when it cannot be used.

        A broken contract must fail loudly: treating it as "nothing routable" would
        quietly plan zero knowledge documents.
        """
        stat = self.schema_path.stat()
        if self._cache and self._cache[0] == stat.st_mtime:
            data = self._cache[1]
            for key in required_blocks:
                if not isinstance(data.get(key), dict):
                    raise WikiGateError(f"{self.schema_path} is missing machine block '{key}'")
            return data
        text = self.schema_path.read_text(encoding="utf-8", errors="replace")
        fm_text, _ = split_frontmatter(text)
        if fm_text is None:
            raise WikiGateError(f"{self.schema_path} has no parseable frontmatter")
        data = yaml.safe_load(fm_text)
        if not isinstance(data, dict):
            raise WikiGateError(f"{self.schema_path} frontmatter is not a mapping")
        for key in required_blocks:
            if not isinstance(data.get(key), dict):
                raise WikiGateError(f"{self.schema_path} is missing machine block '{key}'")
        # A deposit routing the engine cannot honour is a broken contract, not an absent one.
        deposit_routing_rules(data, source=self.schema_path.name)
        self._cache = (stat.st_mtime, data)
        return data

    def projects_root_rel(self, schema: dict[str, Any]) -> str:
        """The path (relative to the Wiki root) of the partition that holds the projects.

        Read off the contract, never hardcoded: a Wiki may declare another root.
        """
        partitions = (schema.get("partition_contract") or {}).get("partitions") or {}
        root_rel = str((partitions.get(PROJECTS_PARTITION_ID) or {}).get("root") or DEFAULT_PROJECTS_ROOT)
        return root_rel.strip("/")

    def deposit_routing_rules(self, schema: dict[str, Any]) -> list[dict[str, Any]]:
        """The contract's project-domain deposit routing; see :func:`deposit_routing_rules`."""
        return deposit_routing_rules(schema, source=self.schema_path.name)
