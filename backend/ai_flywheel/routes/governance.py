"""Governance HTTP routes: thin transport adapter over policies, activation, receipts, and governance services."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_flywheel.core.governance import (
    GovernanceConflictError,
    GovernanceNotFoundError,
    GovernanceValidationError,
)
from ai_flywheel.core.receipts import OutcomeGateError
from ai_flywheel.models import (
    CreateRuleRequest,
    PreflightRequest,
    RecordOutcomeRequest,
    ReportSignalRequest,
    RetroStatus,
    RuleStatus,
)
from ai_flywheel.runtime import FlywheelRuntime


class RuleTransitionBody(BaseModel):
    status: RuleStatus
    note: str = ""


class RetroStatusBody(BaseModel):
    status: RetroStatus


class RetroBulkStatusBody(BaseModel):
    retro_ids: list[str]
    status: RetroStatus


class RetroPromoteBody(BaseModel):
    retro_ids: list[str]
    severity_grade: str | None = None


class ObservationBulkArchiveBody(BaseModel):
    obs_ids: list[str]


class DissentCreateBody(BaseModel):
    target_type: str = "rule"
    target_id: str
    counter_claim: str
    condition_scope: str = ""
    evidence_pointer: str = ""


class DissentStatusBody(BaseModel):
    status: str


class DissentBulkStatusBody(BaseModel):
    dissent_ids: list[str]
    status: str


class KnowledgeFreshnessBody(BaseModel):
    doc_path: str
    action: str
    freshness_tier: str | None = None


def _raise_http(exc: Exception, *, validation_status: int = 422) -> None:
    if isinstance(exc, GovernanceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, GovernanceConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, GovernanceValidationError):
        raise HTTPException(status_code=validation_status, detail=str(exc)) from exc
    raise exc


def build_governance_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    # ---- Policies (Decoupled from Knowledge Base) ----
    @router.get("/api/site/policies")
    def list_policies() -> dict[str, Any]:
        return {"policies": rt.policies.list_policies()}

    # ---- Unified Code Dictionary (read-only display metadata) ----
    @router.get("/api/site/dicts")
    def site_list_dicts(
        dict_type: str | None = None,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        items = rt.db.list_dicts(dict_type=dict_type, include_disabled=include_disabled)
        grouped = rt.db.get_dicts_grouped(
            dict_type=dict_type, include_disabled=include_disabled
        )
        return {
            "dicts": grouped,
            "items": [item.model_dump(mode="json") for item in items],
            "total": len(items),
        }

    @router.put("/api/site/policies/{policy_id}")
    def update_policy(policy_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updated = rt.policies.update_policy(policy_id, patch)
        return {"status": "updated", "policy": updated}

    @router.post("/api/site/policies/{policy_id}/reset")
    def reset_policy(policy_id: str) -> dict[str, Any]:
        restored = rt.policies.reset_policy(policy_id)
        return {"status": "reset", "policy": restored}

    # ---- Interactive Preflight / Outcome / Signal Endpoints ----
    @router.post("/api/site/preflight")
    def site_preflight(req: PreflightRequest) -> dict[str, Any]:
        return rt.activation.preflight_activate(req).model_dump(mode="json")

    @router.post("/api/site/outcomes")
    def site_record_outcome(req: RecordOutcomeRequest) -> JSONResponse:
        try:
            rec = rt.receipts.record_outcome(req)
            return JSONResponse(rec.model_dump(mode="json"), status_code=201)
        except OutcomeGateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/site/outcomes")
    def site_list_outcomes(limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        items = rt.db.list_outcomes(limit=safe_limit)
        return {
            "outcomes": [o.model_dump(mode="json") for o in items],
            "total": rt.db.count_outcomes(),
        }

    # ---- Rules, Mistake Observation Pool, Dissents & Growth Ledger ----
    @router.get("/api/site/rules")
    def site_list_rules(status: str | None = None) -> dict[str, Any]:
        rules = rt.db.list_rules(status=status)
        dissents_map = rt.db.get_open_dissents_by_targets([r.rule_id for r in rules])
        return {
            "rules": [r.model_dump(mode="json") for r in rules],
            "dissents_by_rule": dissents_map,
        }

    @router.post("/api/site/rules")
    def site_create_rule(req: CreateRuleRequest) -> dict[str, Any]:
        rule = rt.activation.register_rule(req)
        rt.refresh_core_card()
        return {"rule": rule.model_dump(mode="json")}

    @router.post("/api/site/rules/{rule_id}/transition")
    def site_transition_rule(rule_id: str, body: RuleTransitionBody) -> dict[str, Any]:
        rule = rt.activation.transition_rule_status(rule_id, body.status, note=body.note)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        rt.refresh_core_card()
        return {"rule": rule.model_dump(mode="json")}

    @router.get("/api/site/observations")
    def site_list_observations(
        status: str | None = None,
        severity_grade: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        items = rt.db.list_mistake_observations(
            status=status, severity_grade=severity_grade, limit=safe_limit
        )
        return {"observations": items, "total": len(items)}

    @router.post("/api/site/observations/bulk-archive")
    def site_bulk_archive_observations(body: ObservationBulkArchiveBody) -> dict[str, Any]:
        return {"updated": rt.governance.bulk_archive_observations(body.obs_ids)}

    @router.post("/api/site/observations/bulk-restore")
    def site_bulk_restore_observations(body: ObservationBulkArchiveBody) -> dict[str, Any]:
        return {"updated": rt.governance.bulk_restore_observations(body.obs_ids)}

    @router.post("/api/site/observations/{obs_id}/promote")
    def site_promote_observation(obs_id: str) -> dict[str, Any]:
        promoted = rt.activation.promote_observation(obs_id)
        if not promoted:
            raise HTTPException(status_code=404, detail="Observation not found")
        updated, rule = promoted
        rt.refresh_core_card()
        return {"observation": updated, "rule": rule.model_dump(mode="json")}

    @router.post("/api/site/observations/{obs_id}/archive")
    def site_archive_observation(obs_id: str) -> dict[str, Any]:
        try:
            return {"observation": rt.governance.archive_observation(obs_id)}
        except Exception as exc:
            _raise_http(exc)

    @router.post("/api/site/observations/{obs_id}/restore")
    def site_restore_observation(obs_id: str) -> dict[str, Any]:
        try:
            return {"observation": rt.governance.restore_observation(obs_id)}
        except Exception as exc:
            _raise_http(exc)

    @router.get("/api/site/dissents")
    def site_list_dissents(
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        dissents = rt.db.list_dissent_records(target_id=target_id, status=status, limit=safe_limit)
        rules_by_id = {r.rule_id: r for r in rt.db.list_rules()}
        enriched = [
            {
                **d,
                "target_title": (rule.title if rule else ""),
                "target_status": (rule.status.value if rule else ""),
            }
            for d in dissents
            for rule in (
                [rules_by_id.get(str(d.get("target_id") or "")) if d.get("target_type") == "rule" else None]
            )
        ]
        return {"dissents": enriched, "total": len(enriched)}

    @router.post("/api/site/dissents")
    def site_create_dissent(body: DissentCreateBody) -> dict[str, Any]:
        if not body.counter_claim.strip():
            raise HTTPException(status_code=422, detail="counter_claim is required")
        rec = rt.db.add_dissent_record(
            target_type=body.target_type,
            target_id=body.target_id,
            counter_claim=body.counter_claim,
            condition_scope=body.condition_scope,
            evidence_pointer=body.evidence_pointer or "manual-portal",
        )
        rt.refresh_core_card()
        return {"dissent": rec}

    @router.post("/api/site/dissents/bulk-status")
    def site_bulk_update_dissents(body: DissentBulkStatusBody) -> dict[str, Any]:
        if body.status not in {"open", "merged", "dismissed"}:
            raise HTTPException(status_code=422, detail="Invalid dissent status")
        updated = rt.db.bulk_update_dissent_status(body.dissent_ids, body.status)
        if updated > 0:
            rt.refresh_core_card()
        return {"updated": updated}

    @router.post("/api/site/dissents/{dissent_id}/status")
    def site_update_dissent(dissent_id: str, body: DissentStatusBody) -> dict[str, Any]:
        updated = rt.db.update_dissent_status(dissent_id, body.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Dissent not found or invalid status")
        rt.refresh_core_card()
        return {"dissent": updated}

    @router.get("/api/site/growth-metrics")
    def site_growth_metrics() -> dict[str, Any]:
        return {
            "live": rt.db.compute_live_growth_metrics(),
            "latest_snapshot": rt.db.get_latest_metrics_snapshot(),
            "history": rt.db.list_metrics_snapshots(limit=12),
        }

    @router.get("/api/site/core-card")
    def site_core_card() -> dict[str, Any]:
        return rt.read_core_card()

    @router.post("/api/site/core-card/refresh")
    def site_core_card_refresh() -> dict[str, Any]:
        return rt.refresh_core_card()

    @router.get("/api/site/retro")
    def site_list_retro(status: str | None = None, limit: int = 300) -> dict[str, Any]:
        return rt.governance.list_enriched_retro(status=status, limit=limit)

    @router.post("/api/site/signals")
    def site_report_signal(req: ReportSignalRequest) -> dict[str, Any]:
        item, created = rt.receipts.report_signal(req)
        return {"created": created, "item": item.model_dump(mode="json")}

    @router.post("/api/site/retro/bulk-status")
    def site_bulk_update_retro(body: RetroBulkStatusBody) -> dict[str, Any]:
        updated = rt.db.bulk_update_retro_status(body.retro_ids, body.status)
        return {"updated": updated}

    @router.post("/api/site/retro/promote")
    def site_promote_retro(body: RetroPromoteBody) -> dict[str, Any]:
        try:
            return rt.governance.promote_retro_items(
                body.retro_ids,
                severity_grade=body.severity_grade,
            )
        except Exception as exc:
            _raise_http(exc, validation_status=400)

    @router.post("/api/site/retro/{retro_id}/status")
    def site_update_retro(retro_id: str, body: RetroStatusBody) -> dict[str, Any]:
        updated = rt.db.update_retro_status(retro_id, body.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Retro item not found")
        return {"item": updated.model_dump(mode="json")}

    # ---- Knowledge Sink Preview (enriched with freshness & effectiveness metrics) ----
    @router.get("/api/site/knowledge")
    def site_list_knowledge(limit: int = 200) -> dict[str, Any]:
        return rt.governance.list_enriched_knowledge(limit=limit)

    @router.post("/api/site/knowledge/freshness")
    def site_update_knowledge_freshness(body: KnowledgeFreshnessBody) -> dict[str, Any]:
        try:
            return rt.governance.update_knowledge_freshness(
                doc_path=body.doc_path,
                action=body.action,
                freshness_tier=body.freshness_tier,
            )
        except Exception as exc:
            _raise_http(exc)

    @router.get("/api/site/knowledge/doc")
    def site_read_knowledge(path: str) -> dict[str, Any]:
        doc = rt.knowledge.read_document(path)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    # ---- Skill Effectiveness Ledger & Read-Only Preview ----
    @router.get("/api/site/skills")
    def site_list_skills() -> dict[str, Any]:
        return rt.governance.list_merged_skills()

    @router.get("/api/site/skills/{skill_name}/preview")
    def site_skill_preview(skill_name: str) -> dict[str, Any]:
        info = rt.skill_writer.inspect_existing_skill(skill_name)
        if not info.get("exists"):
            raise HTTPException(status_code=404, detail="Skill not found")
        return info

    return router
