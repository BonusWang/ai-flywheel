"""Governance: policies, rules, mistake observations, dissents, knowledge and skills."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


class DissentCreateBody(BaseModel):
    target_type: str = "rule"
    target_id: str
    counter_claim: str
    condition_scope: str = ""
    evidence_pointer: str = ""


class DissentStatusBody(BaseModel):
    status: str


def build_governance_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    # ---- Policies (Decoupled from Knowledge Base) ----
    @router.get("/api/site/policies")
    def list_policies() -> dict[str, Any]:
        return {"policies": rt.policies.list_policies()}

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
    def site_list_outcomes() -> dict[str, Any]:
        return {"outcomes": [o.model_dump(mode="json") for o in rt.db.list_outcomes(limit=50)]}

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
    ) -> dict[str, Any]:
        items = rt.db.list_mistake_observations(status=status, severity_grade=severity_grade, limit=120)
        return {"observations": items}

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
        updated = rt.db.update_mistake_observation_status(obs_id, "archived")
        if not updated:
            raise HTTPException(status_code=404, detail="Observation not found")
        rt.refresh_core_card()
        return {"observation": updated}

    @router.get("/api/site/dissents")
    def site_list_dissents(target_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        return {"dissents": rt.db.list_dissent_records(target_id=target_id, status=status, limit=120)}

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
        # Read-only: a plain GET never rewrites the derived card on disk.
        return rt.read_core_card()

    @router.post("/api/site/core-card/refresh")
    def site_core_card_refresh() -> dict[str, Any]:
        # The explicit rebuild: the card is a projection of the store, so it is
        # only rewritten when the caller asks for it.
        return rt.refresh_core_card()

    @router.get("/api/site/retro")
    def site_list_retro(status: str | None = None) -> dict[str, Any]:
        return {"items": [x.model_dump(mode="json") for x in rt.db.list_retro_items(status=status, limit=100)]}

    @router.post("/api/site/signals")
    def site_report_signal(req: ReportSignalRequest) -> dict[str, Any]:
        item, created = rt.receipts.report_signal(req)
        return {"created": created, "item": item.model_dump(mode="json")}

    @router.post("/api/site/retro/{retro_id}/status")
    def site_update_retro(retro_id: str, body: RetroStatusBody) -> dict[str, Any]:
        updated = rt.db.update_retro_status(retro_id, body.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Retro item not found")
        return {"item": updated.model_dump(mode="json")}

    # ---- Knowledge Sink Preview (enriched with freshness & effectiveness metrics) ----
    @router.get("/api/site/knowledge")
    def site_list_knowledge() -> dict[str, Any]:
        docs = rt.knowledge.list_documents(limit=100)
        freshness_map = rt.db.get_knowledge_freshness_map()
        enriched: list[dict[str, Any]] = []
        for doc in docs:
            finfo = freshness_map.get(doc["doc_path"]) or {}
            enriched.append(
                {
                    **doc,
                    "freshness_tier": finfo.get("freshness_tier", "evolving"),
                    "last_verified_at": finfo.get("last_verified_at"),
                    "valid_until": finfo.get("valid_until"),
                    "freshness_state": finfo.get("freshness_state", "fresh"),
                    "is_expired": bool(finfo.get("is_expired", False)),
                    "surface_count": int(finfo.get("surface_count") or 0),
                    "helped_count": int(finfo.get("helped_count") or 0),
                }
            )
        return {
            "sink_type": rt.cfg.knowledge_sink.type,
            "root": str(rt.knowledge.root),
            "documents": enriched,
        }

    @router.get("/api/site/knowledge/doc")
    def site_read_knowledge(path: str) -> dict[str, Any]:
        doc = rt.knowledge.read_document(path)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    # ---- Skill Effectiveness Ledger & Read-Only Preview ----
    @router.get("/api/site/skills")
    def site_list_skills() -> dict[str, Any]:
        return {
            "skills": rt.db.list_skill_assets(),
            "probed_count": len(rt.activation.probe_local_skills()),
        }

    @router.get("/api/site/skills/{skill_name}/preview")
    def site_skill_preview(skill_name: str) -> dict[str, Any]:
        info = rt.skill_writer.inspect_existing_skill(skill_name)
        if not info.get("exists"):
            raise HTTPException(status_code=404, detail="Skill not found")
        return info

    return router
