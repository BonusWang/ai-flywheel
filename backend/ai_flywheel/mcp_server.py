from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer

from ai_flywheel.core.receipts import OutcomeGateError
from ai_flywheel.models import (
    CheckResult,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeStatus,
    PreflightRequest,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    ReportSignalRequest,
    SignalKind,
    VerificationKind,
)

if TYPE_CHECKING:
    from ai_flywheel.runtime import FlywheelRuntime


def build_mcp_server(runtime: FlywheelRuntime) -> MCPServer:
    """Create the official MCP Server exposing 6 core tools + 3 read-only resources."""
    mcp = MCPServer(
        name="ai-flywheel",
        title="AI Collaboration Flywheel",
        version="0.1.0",
        instructions=(
            "Evidence-driven AI collaboration flywheel. "
            "Call flywheel_preflight_activate before non-trivial tasks to receive anti-laziness "
            "policies, collaboration preferences, and matched precedent rules; "
            "call flywheel_register_session or flywheel_ingest_session to enqueue sessions for "
            "background P00-P07 review; call flywheel_record_outcome with real evidence on major deliveries."
        ),
    )

    @mcp.tool()
    def flywheel_register_session(
        client: str,
        session_id: str,
        title: str = "",
        project: str = "generic",
        workspace: str = "",
        transcript_path: str | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register an AI tool session (with optional local transcript path or summary) into the flywheel queue."""
        rec = runtime.register_session(
            RegisterSessionRequest(
                client=client,
                session_id=session_id,
                title=title,
                project=project,
                workspace=workspace,
                transcript_path=transcript_path,
                summary=summary,
                tags=tags or [],
            )
        )
        return {
            "status": "registered",
            "session_key": rec.session_key,
            "priority": rec.priority,
            "queue_status": rec.status.value,
        }

    @mcp.tool()
    def flywheel_ingest_session(
        client: str,
        session_id: str,
        title: str,
        summary: str,
        content: str = "",
        project: str = "generic",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Push session summary or conversation digest directly (ideal for MCP-only clients like Claude Desktop)."""
        rec = runtime.register_session(
            RegisterSessionRequest(
                client=client,
                session_id=session_id,
                title=title,
                project=project,
                summary=summary,
                content=content or summary,
                tags=tags or [],
            )
        )
        return {
            "status": "ingested",
            "session_key": rec.session_key,
            "priority": rec.priority,
            "queue_status": rec.status.value,
        }

    @mcp.tool()
    def flywheel_preflight_activate(
        task: str,
        project: str = "generic",
        client: str = "generic",
        session_id: str = "",
        turn_id: str = "",
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Activate flywheel methodology policies (anti-laziness + preferences), matched rules, and immutable receipt ID."""
        resp = runtime.activation.preflight_activate(
            PreflightRequest(
                task=task,
                project=project,
                client=client,
                session_id=session_id,
                turn_id=turn_id,
                mode=mode,
            )
        )
        return resp.model_dump(mode="json")

    @mcp.tool()
    def flywheel_report_signal(
        kind: str,
        summary: str,
        client: str = "generic",
        session_id: str = "",
        session_key: str = "",
        turn_id: str = "",
        severity: str = "warning",
        tool_name: str | None = None,
        rule_id: str | None = None,
    ) -> dict[str, Any]:
        """Report an execution anomaly (retry_loop, high_risk_tool, missing_evidence, rule_recurrence, blocker, custom)."""
        try:
            signal_kind = SignalKind(kind)
        except ValueError:
            signal_kind = SignalKind.CUSTOM
        item, created = runtime.receipts.report_signal(
            ReportSignalRequest(
                session_key=session_key,
                client=client,
                session_id=session_id,
                turn_id=turn_id,
                kind=signal_kind,
                severity=severity,
                summary=summary,
                tool_name=tool_name,
                rule_id=rule_id,
            )
        )
        return {
            "status": "enqueued" if created else "deduplicated",
            "retro_id": item.retro_id,
            "kind": item.kind.value,
            "rule_id": item.rule_id,
        }

    @mcp.tool()
    def flywheel_record_outcome(
        status: str,
        summary: str,
        checks: list[dict[str, Any]],
        applied_rule_ids: list[str] | None = None,
        client: str = "generic",
        session_id: str = "",
        session_key: str = "",
        turn_id: str = "",
        preflight_receipt_id: str | None = None,
        origin: str = "natural",
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit structured task verification outcome with hard gate validation to drive two-tier rule revalidation."""
        parsed_checks: list[OutcomeCheck] = []
        for raw in checks:
            parsed_checks.append(
                OutcomeCheck(
                    name=str(raw.get("name", "unnamed-check")),
                    result=CheckResult(str(raw.get("result", "unknown"))),
                    kind=VerificationKind(str(raw.get("kind", "unknown"))),
                    required=bool(raw.get("required", True)),
                    evidence=[str(e) for e in raw.get("evidence", [])],
                    rule_id=raw.get("rule_id"),
                    note=str(raw.get("note", "")),
                )
            )
        try:
            rec = runtime.receipts.record_outcome(
                RecordOutcomeRequest(
                    session_key=session_key,
                    client=client,
                    session_id=session_id,
                    turn_id=turn_id,
                    preflight_receipt_id=preflight_receipt_id,
                    origin=OutcomeOrigin(origin),
                    status=OutcomeStatus(status),
                    summary=summary,
                    checks=parsed_checks,
                    applied_rule_ids=applied_rule_ids or [],
                    artifacts=artifacts or [],
                )
            )
            return {
                "status": "recorded",
                "outcome_id": rec.outcome_id,
                "receipt_hash": rec.receipt_hash,
                "revalidation_effects": rec.revalidation_effects,
            }
        except (OutcomeGateError, ValueError) as exc:
            return {
                "status": "rejected",
                "error": str(exc),
                "message": "Invalid outcome, source, or evidence; no completion was recorded.",
            }

    @mcp.tool()
    def flywheel_status() -> dict[str, Any]:
        """Inspect current flywheel metrics, active rule distribution, open retro count, and latest P00-P07 run."""
        return runtime.publication.get_overview(public_mode=False)

    @mcp.resource("flywheel://policies/active")
    def resource_active_policies() -> str:
        """All active flywheel methodology policies (anti-laziness, loop contract, hard gates, user preferences)."""
        return json.dumps(runtime.policies.list_policies(), ensure_ascii=False, indent=2)

    @mcp.resource("flywheel://rules/active")
    def resource_active_rules() -> str:
        """Currently active precedent rules (unverified_active and verified_active)."""
        rules = [
            r.model_dump(mode="json")
            for r in runtime.db.list_rules()
            if r.status.value in {"unverified_active", "verified_active"}
        ]
        return json.dumps(rules, ensure_ascii=False, indent=2)

    @mcp.resource("flywheel://runs/latest")
    def resource_latest_run() -> str:
        """Latest P00-P07 autonomous workflow run report."""
        runs = runtime.db.list_workflow_runs(limit=1)
        data = runs[0].model_dump(mode="json") if runs else None
        return json.dumps(data, ensure_ascii=False, indent=2)

    return mcp
