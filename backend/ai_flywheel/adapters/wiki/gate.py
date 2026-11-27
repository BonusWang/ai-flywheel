"""The Wiki gate: the contract-driven pre-write and post-write gate.

`WikiGate` composes the modules around it — the schema reader, the validator, the
router and the T5 adapter — and adds the one thing a caller needs: a single object that
knows the mounted Wiki and answers "may this document be written, and where?".
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.adapters.wiki.domain_pages import page_section_sketch
from ai_flywheel.adapters.wiki.postflight import (
    resync_after_rollback,
    run_postflight,
    run_topic_lint,
)
from ai_flywheel.adapters.wiki.practice_index import (
    PracticeIndexRegistration,
    plan_practice_index_registration,
)
from ai_flywheel.adapters.wiki.router import (
    RouteContext,
    RouteDecision,
    RoutingCandidates,
    DepositRouting,
    IncidentRouting,
    _domain_path_parts,
    _domain_template,
    _frontmatter_field,
    discover_practice_projects,
    evaluate_route,
    routable_page_types,
    select_practice_project,
)
from ai_flywheel.adapters.wiki.schema import (
    SCHEMA_FILE_NAME,
    WikiGateError,
    WikiSchemaLoader,
    normalize_relative_path,
)
from ai_flywheel.adapters.wiki.t5_adapter import _CARD_BODY_MAPPERS, normalize_planned_document
from ai_flywheel.adapters.wiki.validator import WikiGateResult, validate_document
from ai_flywheel.config import FlywheelConfig, KnowledgeSinkConfig
from ai_flywheel.models import INCIDENT_PAGE_TYPE, SENSITIVE_CONFIG_PAGE_TYPE, declares_private_page


class WikiGate:
    """Contract-driven pre-write and post-write gate for a schema-governed Wiki."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg
        self._schema_loader: WikiSchemaLoader | None = None

    @property
    def root(self) -> Path:
        return self.cfg.knowledge_root_path

    @property
    def schema_path(self) -> Path:
        return self.root / SCHEMA_FILE_NAME

    @property
    def sink(self) -> KnowledgeSinkConfig:
        """The current sink configuration.

        Read from the config every time: a runtime configuration update replaces
        `knowledge_sink`, and a gate that kept the old object would write under the
        rules the user just changed.
        """
        return self.cfg.knowledge_sink

    @property
    def applicable(self) -> bool:
        return bool(self.sink.wiki_gate_enabled) and self.schema_path.is_file()

    @property
    def schema_loader(self) -> WikiSchemaLoader:
        """The schema reader for the current root; the root follows the configuration."""
        loader = self._schema_loader
        if loader is None or loader.root != self.root:
            loader = WikiSchemaLoader(self.root)
            self._schema_loader = loader
        return loader

    def _load_schema(self) -> dict[str, Any]:
        return self.schema_loader.load()

    @staticmethod
    def _normalize(relative_path: str) -> str:
        return normalize_relative_path(relative_path)

    # ---------------- pre-write validation ----------------

    def validate_document(self, relative_path: str, content: str) -> WikiGateResult:
        """Check one document against the mounted contract."""
        return validate_document(
            applicable=self.applicable,
            schema_loader=self._load_schema,
            relative_path=relative_path,
            content=content,
        )

    # ---------------- knowledge-document routing ----------------

    def routing_candidates(self) -> RoutingCandidates:
        """Discover the routing candidate set from the mounted Wiki; empty when nothing is routable.

        Raises ``WikiGateError`` when the schema cannot be read. A broken contract must fail the run
        loudly: treating it as "nothing routable" would quietly plan zero knowledge documents.
        """
        if not self.applicable:
            return RoutingCandidates()
        return self._routing_candidates(self._load_schema())

    def _routing_candidates(self, schema: dict[str, Any]) -> RoutingCandidates:
        """What the mounted Wiki lets a knowledge document become, and into which projects.

        Practice card types are always offered. The page types of the Wiki's declared
        project-domain deposit routing are offered with them, each project carrying the
        domain pages it already holds. The incident ledger is offered only while the sink
        is allowed to write into it: it is closed until the configuration opens it.
        """
        root_rel, projects_root = self._projects_root(schema)
        deposit = DepositRouting.from_schema(schema)
        if deposit is not None:
            deposit.projects_root = root_rel
        incident = IncidentRouting.from_schema(schema, sink=self.sink)
        card_types = self._routable_page_types(schema)
        return RoutingCandidates(
            page_types=list(
                dict.fromkeys(
                    [
                        *card_types,
                        *(deposit.page_types if deposit is not None else []),
                        *([INCIDENT_PAGE_TYPE] if incident is not None and incident.writable else []),
                    ]
                )
            ),
            projects=self._discover_practice_projects(
                projects_root, domain_subdirs=deposit.subdirs if deposit is not None else ()
            ),
            evidence_page_types=card_types,
            deposit_routing=deposit,
            incident=incident,
        )

    def domain_page_template(self, relative_path: str, content: str = "") -> dict[str, Any] | None:
        """The page template a project-domain page follows, or None when it is not one.

        The path names the domain (its directory under the project) and the page's own
        ``type`` the page type; together they pick the template whose sections an increment
        may fill. ``content`` is the page as it exists: a domain that holds several templates
        is told apart by the page's ``knowledge_kind``.
        """
        if not self.applicable:
            return None
        try:
            schema = self._load_schema()
            deposit = DepositRouting.from_schema(schema)
            if deposit is None:
                return None
            parts = _domain_path_parts(self._normalize(relative_path), self.schema_loader.projects_root_rel(schema))
        except (OSError, WikiGateError, yaml.YAMLError):
            return None
        if parts is None:
            return None
        domain = next(
            (item for item in deposit.domains_for(_frontmatter_field(content, "type")) if item["subdir"] == parts[1]),
            None,
        )
        if domain is None:
            return None
        template, _problem = _domain_template(domain, content)
        return template

    def _projects_root(self, schema: dict[str, Any]) -> tuple[str, Path]:
        root_rel = self.schema_loader.projects_root_rel(schema)
        return root_rel, self.root / root_rel

    def _routable_page_types(self, schema: dict[str, Any]) -> list[str]:
        return routable_page_types(schema, _CARD_BODY_MAPPERS)

    def _discover_practice_projects(
        self, projects_root: Path, *, domain_subdirs: Any = ()
    ) -> list[dict[str, Any]]:
        return discover_practice_projects(projects_root, domain_subdirs=domain_subdirs)

    def _route_context(self, schema: dict[str, Any], candidates: RoutingCandidates) -> RouteContext:
        return RouteContext(
            schema=schema,
            candidates=candidates,
            projects_root_rel=self.schema_loader.projects_root_rel(schema),
            sink=self.sink,
            validate_document=self.validate_document,
            read_document=self.read_domain_page,
        )

    def read_domain_page(self, relative_path: str) -> str | None:
        """A project-domain page as it is on disk, or None when it does not exist.

        Only pages under the Wiki root are read. A page the document surface keeps out of
        default view (a private configuration page) is read here and nowhere else: an
        increment is merged into the page itself, and the result goes back to the same file.
        """
        rel = self._normalize(relative_path)
        if not rel:
            return None
        try:
            target = (self.root / rel).resolve()
            if not target.is_relative_to(self.root.resolve()) or not target.is_file():
                return None
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def is_sensitive_page(
        self, relative_path: str, content: str = "", *, existing_text: str | None = None
    ) -> bool:
        """Whether a write touches a page that must never be written without a human.

        The contract decides, on three grounds: the page is the Wiki's project configuration
        page (its page type, or a deposit domain the contract marks sensitive); or the page,
        the existing one or the draft, declares itself private.
        """
        if not self.applicable:
            return False
        page_type = _frontmatter_field(existing_text or "", "type") or _frontmatter_field(content, "type")
        if page_type == SENSITIVE_CONFIG_PAGE_TYPE:
            return True
        if declares_private_page(existing_text or "") or declares_private_page(content):
            return True
        try:
            schema = self._load_schema()
            deposit = DepositRouting.from_schema(schema)
            if deposit is None:
                return False
            parts = _domain_path_parts(self._normalize(relative_path), self.schema_loader.projects_root_rel(schema))
        except (OSError, WikiGateError, yaml.YAMLError):
            return False
        return parts is not None and parts[1] in deposit.sensitive_subdirs()

    def sketch_domain_pages(self, candidates: RoutingCandidates, names: list[Any]) -> None:
        """Give each candidate project the section layout of the domain pages a plan may update.

        A planner cannot write rows that fit a table it has never seen. For every name the
        review signalled that is an existing page of a project, the project gains
        ``domain_page_sections``: each section's table header, or None where the section holds
        text. Only that structure is read, never a data row, so a page that declares itself
        private lends its layout too: its increments have to fit the same tables.
        """
        wanted = {str(name).strip().lower() for name in names if str(name or "").strip()}
        if not wanted or not self.applicable:
            return
        try:
            schema = self._load_schema()
            root_rel = self.schema_loader.projects_root_rel(schema)
        except (OSError, WikiGateError, yaml.YAMLError):
            return
        for project in candidates.projects:
            for subdir, pages in (project.get("domain_inventory") or {}).items():
                for page in pages:
                    if page.lower() not in wanted:
                        continue
                    text = self.read_domain_page(f"{root_rel}/{project['dir_name']}/{subdir}/{page}.md")
                    if text is None:
                        continue
                    project.setdefault("domain_page_sections", {})[page] = page_section_sketch(text)

    def evaluate_route(
        self,
        *,
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
        ``action`` matters for a project-domain page only, whose ``source_sessions`` a new
        page records as its provenance.
        """
        if not self.applicable:
            return RouteDecision(routed=True, passthrough=True)
        try:
            schema = self._load_schema()
        except (OSError, WikiGateError, yaml.YAMLError):
            return RouteDecision(routed=True, passthrough=True)
        if candidates is None:
            candidates = self._routing_candidates(schema)
        return evaluate_route(
            context=self._route_context(schema, candidates),
            relative_path=relative_path,
            title=title,
            summary=summary,
            content=content,
            wiki_route=wiki_route,
            source_workspaces=source_workspaces,
            project_hint=project_hint,
            allow_evidence_fallback=allow_evidence_fallback,
            candidates=candidates,
            action=action,
            source_sessions=source_sessions or [],
        )

    def _select_practice_project(
        self,
        candidates: list[dict[str, Any]],
        haystack: str,
        workspaces: list[str],
    ) -> dict[str, Any] | None:
        return select_practice_project(
            candidates,
            haystack,
            workspaces,
            fallback_project=self.sink.practice_fallback_project,
        )

    def normalize_planned_document(
        self,
        relative_path: str | None,
        title: str,
        summary: str,
        content: str,
        *,
        source_sessions: list[str] | None = None,
        project_hint: str = "",
        source_workspaces: list[str] | None = None,
        wiki_route: dict[str, Any] | None = None,
        route: RouteDecision | None = None,
        allow_evidence_fallback: bool = True,
        action: str = "create",
    ) -> tuple[str, str] | None:
        """Adapt a generic P03 knowledge_doc into a schema-compliant Wiki page (T5 project-practice-note).

        Returns the ``(relative_path, markdown)`` to write, or ``None`` when the document is not
        routed (see :meth:`evaluate_route`): an unattributed or contradicted card is never written.

        ``route`` is a verdict the caller already computed; without it the verdict is computed
        here from the planner's ``wiki_route`` declaration and the evidence arguments.

        A project-domain page (the verdict carries a ``domain``) has no generic draft to
        adapt: an update writes the increment the verdict merged and a create the page the
        verdict built, both free of anything the Wiki already holds. An incident card is
        the page the verdict built as well: the draft under the standard section frame.
        """
        rel = self._normalize(relative_path or "")
        if not self.applicable:
            return rel, content
        try:
            schema = self._load_schema()
        except Exception:
            return rel, content

        # Routing comes first: the verdict says whether the draft lands, and where.
        if route is None:
            route = self.evaluate_route(
                relative_path=rel,
                title=title,
                summary=summary,
                content=content,
                wiki_route=wiki_route,
                source_workspaces=source_workspaces,
                project_hint=project_hint,
                allow_evidence_fallback=allow_evidence_fallback,
                action=action,
                source_sessions=source_sessions,
            )
        if not route.routed:
            return None
        if route.passthrough:
            return rel, content
        if route.domain:
            return rel, (route.increment if route.incremental else route.page)
        if route.page_type == INCIDENT_PAGE_TYPE:
            return rel, route.page

        # If already valid against the mounted schema, keep as-is
        if rel and self.validate_document(rel, content).ok:
            return rel, content

        return normalize_planned_document(
            route=route,
            relative_path=rel,
            title=title,
            summary=summary,
            content=content,
            projects_root_rel=self.schema_loader.projects_root_rel(schema),
            source_sessions=source_sessions,
        )

    # ---------------- post-write validation ----------------

    def plan_practice_index_registration(
        self,
        relative_path: str,
        content: str,
    ) -> PracticeIndexRegistration | None:
        """Return the project-index side effect of a practice-card write."""
        if not self.applicable:
            return None
        return plan_practice_index_registration(self.root, relative_path, content)

    def run_topic_lint(self, result: WikiGateResult, relative_path: str) -> None:
        run_topic_lint(result, relative_path, sink=self.sink, root=self.root)

    def run_postflight(
        self,
        result: WikiGateResult,
        relative_path: str | list[str],
    ) -> None:
        run_postflight(
            result,
            relative_path,
            sink=self.sink,
            root=self.root,
            evidence_dir=self.cfg.wiki_gate_evidence_path,
        )

    def resync_after_rollback(self, relative_path: str | list[str]) -> dict[str, Any]:
        """Re-run index sync only after a failed write has been rolled back."""
        return resync_after_rollback(
            relative_path,
            sink=self.sink,
            root=self.root,
            evidence_dir=self.cfg.wiki_gate_evidence_path,
        )

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
