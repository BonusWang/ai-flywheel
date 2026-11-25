"""Automation scheduler, Friday dream consolidation and the Feishu link self-check."""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai_flywheel.core.dream import preview_dream_consolidation, run_dream_consolidation
from ai_flywheel.runtime import FlywheelRuntime


class FeishuCheckBody(BaseModel):
    wait_seconds: float = 120.0


class AutomationScheduleBody(BaseModel):
    schedule_enabled: bool | None = None
    daily_cron: str | None = None
    friday_weekly_enabled: bool | None = None
    approval_mode: str | None = None


class DreamRunBody(BaseModel):
    dry_run: bool = False


def build_automations_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    # ---- Automation Scheduler & Daemons ----
    @router.get("/api/site/automations")
    def site_get_automations() -> dict[str, Any]:
        return rt.automation.get_snapshot()

    @router.put("/api/site/automations/schedule")
    def site_update_automation_schedule(body: AutomationScheduleBody) -> dict[str, Any]:
        wf_patch: dict[str, Any] = {}
        if body.schedule_enabled is not None:
            wf_patch["schedule_enabled"] = body.schedule_enabled
        if body.daily_cron is not None:
            wf_patch["daily_cron"] = body.daily_cron
        if body.friday_weekly_enabled is not None:
            wf_patch["friday_weekly_enabled"] = body.friday_weekly_enabled
        if body.approval_mode is not None:
            mode = body.approval_mode.strip().lower()
            if mode not in {"smart", "always", "auto"}:
                raise HTTPException(status_code=400, detail=f"Invalid approval_mode: {body.approval_mode}")
            wf_patch["approval_mode"] = mode
        if not wf_patch:
            return {
                "automations": rt.automation.get_snapshot(),
                "restart_required": False,
                "restart_reasons": [],
            }
        try:
            result = rt.update_runtime_config({"workflow": wf_patch})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "automations": rt.automation.get_snapshot(),
            "restart_required": result["restart_required"],
            "restart_reasons": result["restart_reasons"],
        }

    @router.post("/api/site/automations/recover")
    def site_trigger_automation_recovery() -> dict[str, Any]:
        res = rt.automation.run_recovery_now()
        return {
            **res,
            "automations": rt.automation.get_snapshot(),
        }

    @router.post("/api/site/feishu-check")
    def site_feishu_check_start(body: FeishuCheckBody | None = None) -> dict[str, Any]:
        fcfg = rt.cfg.delivery.feishu_cli
        if not fcfg.enabled or not fcfg.user_id or fcfg.identity != "bot":
            raise HTTPException(
                status_code=404,
                detail="Feishu interactive layer is not configured (need enabled feishu_cli with bot identity and a private-chat user_id)",
            )
        if not fcfg.interactive:
            raise HTTPException(
                status_code=400,
                detail="请先开启飞书交互审批卡片（delivery.feishu_cli.interactive）并保存设置，再运行链路自检",
            )
        check = rt.feishu_check
        if check is None:
            raise HTTPException(
                status_code=404,
                detail="Feishu interactive layer is not initialized (check that lark-cli is installed and restart serve if needed)",
            )
        wait = float(body.wait_seconds if body is not None else 120.0)
        threading.Thread(
            target=lambda: check.run(wait_seconds=wait),
            name="flywheel-feishu-check",
            daemon=True,
        ).start()
        return {"started": True, "wait_seconds": wait}

    @router.get("/api/site/feishu-check")
    def site_feishu_check_status() -> dict[str, Any]:
        fcfg = rt.cfg.delivery.feishu_cli
        base_meta = {
            "enabled": bool(fcfg.enabled),
            "interactive": bool(fcfg.interactive),
            "interaction_ready": bool(fcfg.interaction_ready),
            "availability": (
                rt.feishu_channel.availability()
                if rt.feishu_channel is not None
                else ("disabled" if not fcfg.enabled else "down:unconfigured")
            ),
        }
        check = rt.feishu_check
        if check is None:
            return {
                **base_meta,
                "state": "unavailable",
                "reason": "feishu interactive layer not configured",
                "running": False,
                "valid": False,
                "listener": None,
            }
        status = check.status()
        listener = rt.feishu_listener
        status["listener"] = listener.state if listener is not None else None
        status.update(base_meta)
        return status

    # ---- Friday Dream Consolidation (read-only preview & explicit run) ----
    @router.get("/api/site/dream")
    def site_dream_status() -> dict[str, Any]:
        recent_events = rt.db.list_audit_events(entity_type="dream_consolidation", limit=5)
        preview = preview_dream_consolidation(rt.cfg, rt.db)
        return {
            "latest_event": recent_events[0] if recent_events else None,
            "recent_events": recent_events,
            "preview": preview,
        }

    @router.post("/api/site/dream/run")
    def site_dream_run(body: DreamRunBody | None = None) -> dict[str, Any]:
        dry_run = bool(body.dry_run) if body is not None else False
        if dry_run:
            result = preview_dream_consolidation(rt.cfg, rt.db)
        else:
            result = run_dream_consolidation(rt.cfg, rt.db, run_id="manual-portal")
            rt.refresh_core_card()
            rt.broker.publish("automation_updated", {"source": "dream", "period_key": result["period_key"]})
        recent_events = rt.db.list_audit_events(entity_type="dream_consolidation", limit=5)
        return {
            "result": result,
            "latest_event": recent_events[0] if recent_events else None,
        }

    return router
