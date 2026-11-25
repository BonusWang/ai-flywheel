"""Public layer, site metadata, connect/settings and the thin-client hook receiver."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ai_flywheel import __version__
from ai_flywheel.core.connect_templates import build_client_connect_guides
from ai_flywheel.core.publication import MCP_TOOL_CATALOG
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.seed import seed_demo_data


def build_overview_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    # ---------------- Public Unified Layer (AIHOT-inspired) ----------------
    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "lock": rt.db.get_workflow_lock()}

    @router.get("/llms.txt", response_class=PlainTextResponse)
    def llms_txt() -> str:
        return rt.publication.build_llms_txt()

    @router.get("/api/v1/overview")
    def api_v1_overview() -> dict[str, Any]:
        return rt.publication.get_overview(public_mode=True)

    @router.get("/api/v1/about")
    def api_v1_about() -> dict[str, Any]:
        return rt.publication.get_about_payload(public_mode=True)

    @router.get("/api/v1/public-snapshot")
    def api_v1_public_snapshot() -> dict[str, Any]:
        return rt.publication.export_public_snapshot()

    # ---------------- Thin Client Hook Receiver ----------------
    @router.post("/api/hooks/{client}")
    async def receive_thin_hook(client: str, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return rt.hooks.handle(client, payload if isinstance(payload, dict) else {})

    # ---------------- Site / Portal API (/api/site/*) ----------------
    @router.get("/api/site/overview")
    def site_overview() -> dict[str, Any]:
        return rt.publication.get_overview(public_mode=False)

    @router.get("/api/site/about")
    def site_about() -> dict[str, Any]:
        return rt.publication.get_about_payload(public_mode=False)

    def _safe_config_dump() -> dict[str, Any]:
        dumped = rt.cfg.model_dump(mode="json")
        api_cfg = dumped.get("runner", {}).get("api")
        if isinstance(api_cfg, dict):
            has_key = bool(api_cfg.get("api_key"))
            api_cfg["api_key"] = ""
            api_cfg["api_key_configured"] = has_key
        webhook_cfg = dumped.get("delivery", {}).get("webhook")
        if isinstance(webhook_cfg, dict):
            has_secret = bool(webhook_cfg.get("secret"))
            webhook_cfg["secret"] = ""
            webhook_cfg["secret_configured"] = has_secret
        return dumped

    @router.get("/api/site/connect")
    def site_connect() -> dict[str, Any]:
        return {
            "guides": build_client_connect_guides(rt.cfg),
            "config": _safe_config_dump(),
            "probed_skills": rt.activation.probe_local_skills(),
            "mcp_tools": MCP_TOOL_CATALOG,
        }

    @router.put("/api/site/config")
    def site_update_config(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            result = rt.update_runtime_config(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "saved",
            "config": _safe_config_dump(),
            "restart_required": result["restart_required"],
            "restart_reasons": result["restart_reasons"],
        }

    @router.post("/api/site/seed")
    def site_seed() -> dict[str, Any]:
        return seed_demo_data(rt.cfg, rt.db, rt.activation, rt.receipts)

    @router.get("/api/site/settings")
    def site_list_settings() -> dict[str, Any]:
        return {
            "settings": rt.db.list_system_settings(),
            "skills": rt.db.list_skill_assets(),
        }

    return router
