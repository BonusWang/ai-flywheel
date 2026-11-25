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
        _, projects_root = self._projects_root(schema)
        return RoutingCandidates(
            page_types=self._routable_page_types(schema),
            projects=self._discover_practice_projects(projects_root),
        )

    def _projects_root(self, schema: dict[str, Any]) -> tuple[str, Path]:
        root_rel = self.schema_loader.projects_root_rel(schema)
        return root_rel, self.root / root_rel

    def _routable_page_types(self, schema: dict[str, Any]) -> list[str]:
        return routable_page_types(schema, _CARD_BODY_MAPPERS)

    def _discover_practice_projects(self, projects_root: Path) -> list[dict[str, Any]]:
        return discover_practice_projects(projects_root)

    def _route_context(self, schema: dict[str, Any], candidates: RoutingCandidates) -> RouteContext:
        return RouteContext(
            schema=schema,
            candidates=candidates,
            projects_root_rel=self.schema_loader.projects_root_rel(schema),
            sink=self.sink,
            validate_document=self.validate_document,
        )

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
    ) -> RouteDecision:
        """Decide, without any model call, whether a knowledge document is written and where.

        The planner's declaration (``wiki_route``) chooses the page type and the project; workspace
        and draft evidence only validate it. Without a declaration only the dry-run planner's
        evidence-based selection applies, and only when ``allow_evidence_fallback`` is set.
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
    ) -> tuple[str, str] | None:
        """Adapt a generic P03 knowledge_doc into a schema-compliant Wiki page (T5 project-practice-note).

        Returns the ``(relative_path, markdown)`` to write, or ``None`` when the document is not
        routed (see :meth:`evaluate_route`): an unattributed or contradicted card is never written.

        ``route`` is a verdict the caller already computed; without it the verdict is computed
        here from the planner's ``wiki_route`` declaration and the evidence arguments.
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
            )
        if not route.routed:
            return None
        if route.passthrough:
            return rel, content

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
