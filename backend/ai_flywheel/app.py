from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from ai_flywheel import __version__
from ai_flywheel.core.connect_templates import build_client_connect_guides
from ai_flywheel.core.orchestrator import ApprovalLockBusy, WorkflowLockError
from ai_flywheel.core.publication import MCP_TOOL_CATALOG
from ai_flywheel.core.receipts import OutcomeGateError
from ai_flywheel.models import (
    CreateRuleRequest,
    PreflightRequest,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    ReportSignalRequest,
    RetroStatus,
    RuleStatus,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.seed import seed_demo_data


class RunTriggerBody(BaseModel):
    phase: str = "daily"
    session_keys: list[str] | None = None
    auto_approve: bool | None = None


class ApprovalBody(BaseModel):
    approved_indices: list[int] | None = None


class RejectBody(BaseModel):
    reason: str = "用户在可视化画布中取消放行"


class FeishuCheckBody(BaseModel):
    wait_seconds: float = 120.0


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


def create_app(
    runtime: FlywheelRuntime | None = None,
    *,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
    auto_seed: bool = False,
    process_role: str | None = None,
) -> FastAPI:
    # The role decides what this process does (the recovery loop, the Feishu
    # listener). An explicitly requested role wins; without one the runtime's
    # own role decides, because the runtime was built for this process.
    effective_role = process_role or (runtime.process_role if runtime is not None else "serve")
    rt = runtime or FlywheelRuntime.create(
        config_path=config_path,
        base_dir=base_dir,
        auto_seed=auto_seed,
        process_role=effective_role,
    )
    mcp_http_app = rt.mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "testserver"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )

    scheduler: BackgroundScheduler | None = None
    recovery_stop: threading.Event | None = None
    recovery_thread: threading.Thread | None = None

    # ---------------- Feishu interactive layer (built in FlywheelRuntime) ----------------
    # The Feishu pieces are read from the runtime at call time, not captured
    # here: the runtime may build (or rebuild) them after the app was created.

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal scheduler, recovery_stop, recovery_thread
        if rt.cfg.workflow.schedule_enabled:
            scheduler = BackgroundScheduler()
            try:
                scheduler.add_job(
                    lambda: rt.orchestrator.start_run(trigger="scheduled", phase="auto"),
                    CronTrigger.from_crontab(rt.cfg.workflow.daily_cron),
                    id="daily_flywheel",
                    replace_existing=True,
                )
                scheduler.start()
            except Exception:
                scheduler = None
        if effective_role == "serve":
            # Recovery loop: reap runs whose executor is gone and reconcile ask cards.
            # First execution happens inside the thread so lifespan startup never blocks.
            recovery_stop = threading.Event()

            def _recovery_loop() -> None:
                interval = float(os.environ.get("FLYWHEEL_RECOVERY_INTERVAL", "30"))
                stale_after = int(os.environ.get("FLYWHEEL_RECOVERY_STALE_SECONDS", "300"))
                while True:
                    try:
                        rt.orchestrator.recover_interrupted_runs(stale_after_seconds=stale_after)
                    except Exception:
                        logging.getLogger("ai_flywheel.recovery").exception("recovery pass failed")
                    if recovery_stop.wait(interval):
                        break

            recovery_thread = threading.Thread(target=_recovery_loop, name="flywheel-recovery", daemon=True)
            recovery_thread.start()
            rt.start_feishu()
        async with rt.mcp.session_manager.run():
            yield
        rt.stop_feishu()
        if recovery_stop is not None:
            recovery_stop.set()
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="AI Flywheel",
        version=__version__,
        description="Evidence-driven AI collaboration flywheel portal, API, and MCP server.",
        lifespan=lifespan,
    )
    app.state.runtime = rt

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Normalize /mcp -> /mcp/ so MCP HTTP clients posting directly to /mcp never hit a 307 redirect
    @app.middleware("http")
    async def normalize_mcp_path(request: Request, call_next):
        if request.scope.get("path") == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    # Mount official MCP Streamable HTTP at /mcp
    app.mount("/mcp", mcp_http_app)

    # ---------------- Public Unified Layer (AIHOT-inspired) ----------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "lock": rt.db.get_workflow_lock()}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    def llms_txt() -> str:
        return rt.publication.build_llms_txt()

    @app.get("/api/v1/overview")
    def api_v1_overview() -> dict[str, Any]:
        return rt.publication.get_overview(public_mode=True)

    @app.get("/api/v1/about")
    def api_v1_about() -> dict[str, Any]:
        return rt.publication.get_about_payload(public_mode=True)

    @app.get("/api/v1/public-snapshot")
    def api_v1_public_snapshot() -> dict[str, Any]:
        return rt.publication.export_public_snapshot()

    # ---------------- Thin Client Hook Receiver ----------------
    @app.post("/api/hooks/{client}")
    async def receive_thin_hook(client: str, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return rt.hooks.handle(client, payload if isinstance(payload, dict) else {})

    # ---------------- Live SSE Stream ----------------
    @app.get("/api/events")
    async def sse_events(request: Request) -> StreamingResponse:
        entry = rt.broker.subscribe()
        _, queue = entry

        async def event_stream():
            try:
                yield "event: connected\ndata: {\"status\":\"connected\"}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield msg
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                rt.broker.unsubscribe(entry)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ---------------- Site / Portal API (/api/site/*) ----------------
    @app.get("/api/site/overview")
    def site_overview() -> dict[str, Any]:
        return rt.publication.get_overview(public_mode=False)

    @app.get("/api/site/about")
    def site_about() -> dict[str, Any]:
        return rt.publication.get_about_payload(public_mode=False)

    def _safe_config_dump() -> dict[str, Any]:
        dumped = rt.cfg.model_dump(mode="json")
        api_cfg = dumped.get("runner", {}).get("api")
        if isinstance(api_cfg, dict):
            has_key = bool(api_cfg.get("api_key"))
            api_cfg["api_key"] = ""
            api_cfg["api_key_configured"] = has_key
        return dumped

    @app.get("/api/site/connect")
    def site_connect() -> dict[str, Any]:
        return {
            "guides": build_client_connect_guides(rt.cfg),
            "config": _safe_config_dump(),
            "probed_skills": rt.activation.probe_local_skills(),
            "mcp_tools": MCP_TOOL_CATALOG,
        }

    @app.put("/api/site/config")
    def site_update_config(patch: dict[str, Any]) -> dict[str, Any]:
        result = rt.update_runtime_config(patch)
        return {
            "status": "saved",
            "config": _safe_config_dump(),
            "restart_required": result["restart_required"],
            "restart_reasons": result["restart_reasons"],
        }

    @app.post("/api/site/seed")
    def site_seed() -> dict[str, Any]:
        return seed_demo_data(rt.cfg, rt.db, rt.activation, rt.receipts)

    # ---- Policies (Decoupled from Knowledge Base) ----
    @app.get("/api/site/policies")
    def list_policies() -> dict[str, Any]:
        return {"policies": rt.policies.list_policies()}

    @app.put("/api/site/policies/{policy_id}")
    def update_policy(policy_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updated = rt.policies.update_policy(policy_id, patch)
        return {"status": "updated", "policy": updated}

    @app.post("/api/site/policies/{policy_id}/reset")
    def reset_policy(policy_id: str) -> dict[str, Any]:
        restored = rt.policies.reset_policy(policy_id)
        return {"status": "reset", "policy": restored}

    # ---- Interactive Preflight / Outcome / Signal Endpoints ----
    @app.post("/api/site/preflight")
    def site_preflight(req: PreflightRequest) -> dict[str, Any]:
        return rt.activation.preflight_activate(req).model_dump(mode="json")

    @app.post("/api/site/outcomes")
    def site_record_outcome(req: RecordOutcomeRequest) -> JSONResponse:
        try:
            rec = rt.receipts.record_outcome(req)
            return JSONResponse(rec.model_dump(mode="json"), status_code=201)
        except OutcomeGateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/site/outcomes")
    def site_list_outcomes() -> dict[str, Any]:
        return {"outcomes": [o.model_dump(mode="json") for o in rt.db.list_outcomes(limit=50)]}

    # ---- Sessions ----
    @app.get("/api/site/sessions")
    def site_list_sessions(status: str | None = None) -> dict[str, Any]:
        return {"sessions": [s.model_dump(mode="json") for s in rt.db.list_sessions(status=status, limit=100)]}

    @app.post("/api/site/sessions")
    def site_register_session(req: RegisterSessionRequest) -> dict[str, Any]:
        rec = rt.register_session(req)
        return {"session": rec.model_dump(mode="json")}

    # ---- Rules, Mistake Observation Pool, Dissents & Growth Ledger ----
    @app.get("/api/site/rules")
    def site_list_rules(status: str | None = None) -> dict[str, Any]:
        rules = rt.db.list_rules(status=status)
        dissents_map = rt.db.get_open_dissents_by_targets([r.rule_id for r in rules])
        return {
            "rules": [r.model_dump(mode="json") for r in rules],
            "dissents_by_rule": dissents_map,
        }

    @app.post("/api/site/rules")
    def site_create_rule(req: CreateRuleRequest) -> dict[str, Any]:
        rule = rt.activation.register_rule(req)
        rt.refresh_core_card()
        return {"rule": rule.model_dump(mode="json")}

    @app.post("/api/site/rules/{rule_id}/transition")
    def site_transition_rule(rule_id: str, body: RuleTransitionBody) -> dict[str, Any]:
        rule = rt.activation.transition_rule_status(rule_id, body.status, note=body.note)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        rt.refresh_core_card()
        return {"rule": rule.model_dump(mode="json")}

    @app.get("/api/site/observations")
    def site_list_observations(
        status: str | None = None,
        severity_grade: str | None = None,
    ) -> dict[str, Any]:
        items = rt.db.list_mistake_observations(status=status, severity_grade=severity_grade, limit=120)
        return {"observations": items}

    @app.post("/api/site/observations/{obs_id}/promote")
    def site_promote_observation(obs_id: str) -> dict[str, Any]:
        promoted = rt.activation.promote_observation(obs_id)
        if not promoted:
            raise HTTPException(status_code=404, detail="Observation not found")
        updated, rule = promoted
        rt.refresh_core_card()
        return {"observation": updated, "rule": rule.model_dump(mode="json")}

    @app.post("/api/site/observations/{obs_id}/archive")
    def site_archive_observation(obs_id: str) -> dict[str, Any]:
        updated = rt.db.update_mistake_observation_status(obs_id, "archived")
        if not updated:
            raise HTTPException(status_code=404, detail="Observation not found")
        rt.refresh_core_card()
        return {"observation": updated}

    @app.get("/api/site/dissents")
    def site_list_dissents(target_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        return {"dissents": rt.db.list_dissent_records(target_id=target_id, status=status, limit=120)}

    @app.post("/api/site/dissents")
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

    @app.post("/api/site/dissents/{dissent_id}/status")
    def site_update_dissent(dissent_id: str, body: DissentStatusBody) -> dict[str, Any]:
        updated = rt.db.update_dissent_status(dissent_id, body.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Dissent not found or invalid status")
        rt.refresh_core_card()
        return {"dissent": updated}

    @app.get("/api/site/growth-metrics")
    def site_growth_metrics() -> dict[str, Any]:
        return {
            "live": rt.db.compute_live_growth_metrics(),
            "latest_snapshot": rt.db.get_latest_metrics_snapshot(),
            "history": rt.db.list_metrics_snapshots(limit=12),
        }

    @app.get("/api/site/core-card")
    def site_core_card() -> dict[str, Any]:
        # Read-only: a plain GET never rewrites the derived card on disk.
        return rt.read_core_card()

    @app.post("/api/site/core-card/refresh")
    def site_core_card_refresh() -> dict[str, Any]:
        # The explicit rebuild: the card is a projection of the store, so it is
        # only rewritten when the caller asks for it.
        return rt.refresh_core_card()

    @app.get("/api/site/retro")
    def site_list_retro(status: str | None = None) -> dict[str, Any]:
        return {"items": [x.model_dump(mode="json") for x in rt.db.list_retro_items(status=status, limit=100)]}

    @app.post("/api/site/signals")
    def site_report_signal(req: ReportSignalRequest) -> dict[str, Any]:
        item, created = rt.receipts.report_signal(req)
        return {"created": created, "item": item.model_dump(mode="json")}

    @app.post("/api/site/retro/{retro_id}/status")
    def site_update_retro(retro_id: str, body: RetroStatusBody) -> dict[str, Any]:
        updated = rt.db.update_retro_status(retro_id, body.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Retro item not found")
        return {"item": updated.model_dump(mode="json")}

    # ---- P00-P07 Workflow Runs ----
    @app.get("/api/site/runs")
    def site_list_runs() -> dict[str, Any]:
        return {
            "runs": [r.model_dump(mode="json") for r in rt.db.list_workflow_runs(limit=30)],
            "lock": rt.db.get_workflow_lock(),
        }

    @app.get("/api/site/runs/{run_id}")
    def site_get_run(run_id: str) -> dict[str, Any]:
        run = rt.db.get_workflow_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return {
            "run": run.model_dump(mode="json"),
            "events": rt.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=50),
        }

    @app.get("/api/site/runs/{run_id}/events")
    def site_run_events(run_id: str) -> dict[str, Any]:
        return {"events": rt.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=50)}

    @app.post("/api/site/feishu-check")
    def site_feishu_check_start(body: FeishuCheckBody | None = None) -> dict[str, Any]:
        check = rt.feishu_check
        if check is None:
            raise HTTPException(status_code=404, detail="Feishu interactive layer is not configured (need enabled feishu_cli with bot identity and a private-chat user_id)")
        wait = float(body.wait_seconds if body is not None else 120.0)
        threading.Thread(
            target=lambda: check.run(wait_seconds=wait),
            name="flywheel-feishu-check",
            daemon=True,
        ).start()
        return {"started": True, "wait_seconds": wait}

    @app.get("/api/site/feishu-check")
    def site_feishu_check_status() -> dict[str, Any]:
        check = rt.feishu_check
        if check is None:
            return {"state": "unavailable", "reason": "feishu interactive layer not configured", "valid": False}
        status = check.status()
        listener = rt.feishu_listener
        status["listener"] = listener.state if listener is not None else None
        return status

    @app.get("/api/site/settings")
    def site_list_settings() -> dict[str, Any]:
        return {
            "settings": rt.db.list_system_settings(),
            "skills": rt.db.list_skill_assets(),
        }

    @app.post("/api/site/runs")
    def site_start_run(body: RunTriggerBody, background: bool = False) -> dict[str, Any]:
        if background:
            return _start_run_in_background(body)
        try:
            run = rt.orchestrator.start_run(
                trigger="api",
                phase=body.phase,
                session_keys=body.session_keys,
                auto_approve_override=body.auto_approve,
            )
            return {"run": run.model_dump(mode="json")}
        except WorkflowLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _start_run_in_background(body: RunTriggerBody) -> dict[str, Any]:
        """Take the lock now, run the pipeline on a worker thread, answer at once.

        The lock is taken in the request thread so a busy lock is reported as a
        clean 409 instead of a failure nobody sees; the pipeline itself runs on
        a daemon thread and reports through the SSE stream.
        """
        if not rt.cfg.safety_valves.workflow_enabled:
            raise HTTPException(
                status_code=400,
                detail="Workflow execution is disabled by safety valve WORKFLOW_ENABLED=false.",
            )
        run_id = rt.orchestrator.new_run_id()
        if not rt.db.acquire_workflow_lock(run_id):
            lock = rt.db.get_workflow_lock()
            raise HTTPException(
                status_code=409, detail=f"Another workflow run already holds the lock: {lock}"
            )

        def _runner() -> None:
            try:
                rt.orchestrator.start_run(
                    trigger="api",
                    phase=body.phase,
                    session_keys=body.session_keys,
                    auto_approve_override=body.auto_approve,
                    run_id=run_id,
                )
            except Exception as exc:  # the thread is the last line of defence
                logging.getLogger("ai_flywheel.background").exception(
                    "background run %s failed", run_id
                )
                rt.db.release_workflow_lock(run_id)
                rt.broker.publish(
                    "run_failed",
                    {
                        "run_id": run_id,
                        "status": "FAILED",
                        "error": str(exc),
                        "nodes": [],
                        "planned_writes": [],
                    },
                )

        try:
            threading.Thread(target=_runner, name=f"flywheel-run-{run_id}", daemon=True).start()
        except Exception as exc:
            # The worker never started: the lock this request took must not stay.
            rt.db.release_workflow_lock(run_id)
            raise HTTPException(status_code=500, detail=f"Cannot start the background run: {exc}") from exc
        return {"run": None, "run_id": run_id, "background": True, "status": "RUNNING"}

    @app.post("/api/site/runs/{run_id}/approve")
    def site_approve_run(run_id: str, body: ApprovalBody, background: bool = False) -> dict[str, Any]:
        if background:
            return _approve_run_in_background(run_id, body)
        try:
            run = rt.orchestrator.approve_and_resume(run_id, approved_indices=body.approved_indices)
            return {"run": run.model_dump(mode="json")}
        except ApprovalLockBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _approve_run_in_background(run_id: str, body: ApprovalBody) -> dict[str, Any]:
        """Claim the approval now, resume the pipeline on a worker thread.

        The claim is the fast, atomic half — it takes the lock and decides the
        outcome — so a busy lock or a stale run is still reported as a clean
        409 / 404 / 400 before the answer goes out.
        """
        try:
            run = rt.orchestrator.claim_approval_for_resume(
                run_id, approved_indices=body.approved_indices
            )
        except ApprovalLockBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def _runner() -> None:
            try:
                rt.orchestrator.resume_claimed_run(run)
            except Exception as exc:  # the thread is the last line of defence
                logging.getLogger("ai_flywheel.background").exception(
                    "background approval %s failed", run_id
                )
                rt.broker.publish(
                    "run_failed",
                    {
                        "run_id": run_id,
                        "status": "FAILED",
                        "error": str(exc),
                        "nodes": [],
                        "planned_writes": [],
                    },
                )

        try:
            threading.Thread(
                target=_runner, name=f"flywheel-approve-{run_id}", daemon=True
            ).start()
        except Exception as exc:
            # The worker never started: the claimed run must not stay half-open.
            rt.db.finish_run_terminal(
                run_id,
                status="FAILED",
                card_state="failed",
                error=f"background approval could not start: {exc}",
                source="portal",
            )
            raise HTTPException(
                status_code=500, detail=f"Cannot start the background approval: {exc}"
            ) from exc
        return {"run": run.model_dump(mode="json"), "background": True}

    @app.post("/api/site/runs/{run_id}/reject")
    def site_reject_run(run_id: str, body: RejectBody) -> dict[str, Any]:
        try:
            run = rt.orchestrator.reject_run(run_id, reason=body.reason)
            return {"run": run.model_dump(mode="json")}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Knowledge Sink Preview ----
    @app.get("/api/site/knowledge")
    def site_list_knowledge() -> dict[str, Any]:
        return {
            "sink_type": rt.cfg.knowledge_sink.type,
            "root": str(rt.knowledge.root),
            "documents": rt.knowledge.list_documents(limit=100),
        }

    @app.get("/api/site/knowledge/doc")
    def site_read_knowledge(path: str) -> dict[str, Any]:
        doc = rt.knowledge.read_document(path)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    # ---- Skill Preview (read-only): what an existing Skill looks like before it is updated ----
    @app.get("/api/site/skills/{skill_name}/preview")
    def site_skill_preview(skill_name: str) -> dict[str, Any]:
        info = rt.skill_writer.inspect_existing_skill(skill_name)
        if not info.get("exists"):
            raise HTTPException(status_code=404, detail="Skill not found")
        return info

    # ---- Built React Frontend SPA Hosting ----
    frontend_dist = (rt.cfg.base_dir / "frontend" / "dist").resolve()
    if frontend_dist.exists():
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        def serve_spa(full_path: str):
            if full_path.startswith(("api/", "mcp")):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = frontend_dist / full_path
            if full_path and candidate.exists() and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(frontend_dist / "index.html"))

    return app
