from __future__ import annotations

import uuid

from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.privacy import shanghai_today_ymd
from ai_flywheel.models import (
    CheckResult,
    OutcomeRecord,
    OutcomeStatus,
    RecordOutcomeRequest,
    ReportSignalRequest,
    RetroItem,
    SignalKind,
    VerificationKind,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash, redact_text


class OutcomeGateError(ValueError):
    """Raised when record_outcome fails submission-time hard gate validation."""


class ReceiptService:
    """Enforces outcome submission hard gates, immutable receipts, and anomaly retro queueing."""

    def __init__(
        self,
        db: FlywheelDB,
        fs_store: ImmutableFileStore,
        policies: PolicyEngine,
        activation: ActivationEngine,
    ) -> None:
        self.db = db
        self.fs_store = fs_store
        self.policies = policies
        self.activation = activation

    def validate_outcome_request(self, req: RecordOutcomeRequest) -> None:
        gates = self.policies.get_hard_gates().get("outcome_gate", {})
        require_rule_evidence = gates.get("require_evidence_for_applied_rules", True)

        # 1. Submission-time hard gate for applied_rule_ids
        if require_rule_evidence and req.applied_rule_ids:
            checks_by_rule = {c.rule_id: c for c in req.checks if c.rule_id}
            for rid in req.applied_rule_ids:
                chk = checks_by_rule.get(rid)
                if chk is None:
                    raise OutcomeGateError(
                        f"applied_rule_ids contains '{rid}', but no matching check carries rule_id='{rid}'. "
                        "If no rule was exercised, pass applied_rule_ids=[]."
                    )
                if chk.result not in {CheckResult.PASS, CheckResult.FAIL}:
                    raise OutcomeGateError(
                        f"Check for rule '{rid}' must have result 'pass' or 'fail', got '{chk.result.value}'."
                    )
                if not chk.evidence:
                    raise OutcomeGateError(
                        f"Check for rule '{rid}' requires non-empty evidence."
                    )

        # 2. Cannot claim `real` verification without evidence
        for chk in req.checks:
            if chk.kind == VerificationKind.REAL and chk.result == CheckResult.PASS and not chk.evidence:
                raise OutcomeGateError(
                    f"Check '{chk.name}' claims real verification pass without evidence."
                )

        # 3. Overall status == success requires all required checks to pass
        if req.status == OutcomeStatus.SUCCESS:
            required_checks = [c for c in req.checks if c.required]
            for chk in required_checks:
                if chk.result != CheckResult.PASS:
                    raise OutcomeGateError(
                        f"Cannot mark outcome 'success' when required check '{chk.name}' is '{chk.result.value}'. "
                        "Use 'partial', 'failed', or 'unverified'."
                    )

    def record_outcome(self, req: RecordOutcomeRequest) -> OutcomeRecord:
        self.validate_outcome_request(req)

        session_key = req.session_key or (f"{req.client}:{req.session_id}" if req.session_id else f"{req.client}:adhoc")
        outcome_id = f"out-{uuid.uuid4().hex[:12]}"
        used_knowledge_paths = [p for p in dict.fromkeys(req.used_knowledge_paths) if p]
        used_skills = [s.strip() for s in dict.fromkeys(req.used_skills) if s and s.strip()]
        payload = {
            "outcome_id": outcome_id,
            "session_key": session_key,
            "client": req.client,
            "session_id": req.session_id,
            "turn_id": req.turn_id,
            "preflight_receipt_id": req.preflight_receipt_id,
            "origin": req.origin.value,
            "status": req.status.value,
            "summary": redact_text(req.summary),
            "checks": [c.model_dump(mode="json") for c in req.checks],
            "applied_rule_ids": req.applied_rule_ids,
            "used_skills": used_skills,
            "artifacts": req.artifacts,
            "used_knowledge_paths": used_knowledge_paths,
            "created_at": utc_now_iso(),
        }
        _, receipt_hash = self.fs_store.write_receipt("outcomes", outcome_id, payload)

        record = OutcomeRecord(
            outcome_id=outcome_id,
            receipt_hash=receipt_hash,
            session_key=session_key,
            client=req.client,
            preflight_receipt_id=req.preflight_receipt_id,
            origin=req.origin,
            status=req.status,
            summary=redact_text(req.summary),
            checks=req.checks,
            applied_rule_ids=req.applied_rule_ids,
            used_skills=used_skills,
            artifacts=req.artifacts,
            used_knowledge_paths=used_knowledge_paths,
            created_at=payload["created_at"],
        )
        effects = self.activation.revalidate_from_outcome(record)
        record.revalidation_effects = effects
        self.db.save_outcome(record)

        # v3: Attribute Skill invocation & pass/fail outcomes (anti-Skill-theater)
        if used_skills:
            self.db.record_skill_usage(
                used_skills,
                passed=(req.status == OutcomeStatus.SUCCESS),
                at=payload["created_at"],
            )

        # D6 (2026-11-23): helped_count for cards the agent re-read and used.
        # cited_count only when the card also appeared in this task's preflight
        # receipt, so a citation without prior surfacing is still distinguishable.
        if used_knowledge_paths:
            preflight_pointers = set(
                self.db.get_preflight_knowledge_pointers(req.preflight_receipt_id or "")
            )
            cited_paths = [p for p in used_knowledge_paths if p in preflight_pointers]
            self.db.record_knowledge_usage(
                used_knowledge_paths,
                payload["created_at"],
                cited_paths=cited_paths,
            )
            # v3: Auto-renew experience card freshness when verified by at least one real PASS check
            has_real_pass = any(
                c.kind == VerificationKind.REAL and c.result == CheckResult.PASS
                for c in req.checks
            )
            if has_real_pass:
                self.db.refresh_knowledge_freshness(used_knowledge_paths, shanghai_today_ymd())

        # Any failed rule check also enqueues a retro item AND records traceable counter-evidence dissent
        for chk in req.checks:
            if chk.result == CheckResult.FAIL and chk.rule_id:
                claim_text = redact_text(
                    chk.note or req.summary or f"验证项 {chk.name} 未通过"
                )
                self.db.add_dissent_record(
                    target_type="rule",
                    target_id=chk.rule_id,
                    counter_claim=claim_text,
                    condition_scope=f"check={chk.name}",
                    evidence_pointer=outcome_id,
                    session_key=session_key,
                )
                self.report_signal(
                    ReportSignalRequest(
                        session_key=session_key,
                        client=req.client,
                        session_id=req.session_id,
                        turn_id=req.turn_id,
                        kind=SignalKind.RULE_RECURRENCE,
                        severity="high",
                        summary=f"Rule {chk.rule_id} failed verification in check '{chk.name}': {chk.note or req.summary}",
                        rule_id=chk.rule_id,
                        detail={"outcome_id": outcome_id, "evidence": chk.evidence},
                    )
                )
        return record

    def report_signal(self, req: ReportSignalRequest) -> tuple[RetroItem, bool]:
        session_key = req.session_key or (f"{req.client}:{req.session_id}" if req.session_id else f"{req.client}:adhoc")
        dedupe_key = canonical_hash(
            {
                "session_key": session_key,
                "turn_id": req.turn_id,
                "kind": req.kind.value,
                "tool_name": req.tool_name,
                "rule_id": req.rule_id,
                "summary": req.summary[:120],
            }
        )[:24]
        item = RetroItem(
            retro_id=f"retro-{uuid.uuid4().hex[:10]}",
            session_key=session_key,
            client=req.client,
            kind=req.kind,
            severity=req.severity,
            summary=redact_text(req.summary),
            tool_name=req.tool_name,
            rule_id=req.rule_id,
            detail=req.detail,
        )
        saved, created = self.db.enqueue_retro(item, dedupe_key=dedupe_key)
        if created and req.kind == SignalKind.RULE_RECURRENCE and req.rule_id:
            self.activation.mark_rule_recurrence(req.rule_id, req.summary, session_key=session_key)
        return saved, created
