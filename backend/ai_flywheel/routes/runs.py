"""Sessions and P00-P07 runs, including the background start / approval helpers."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai_flywheel.core.orchestrator import ApprovalLockBusy, WorkflowLockError
from ai_flywheel.models import RegisterSessionRequest
from ai_flywheel.runtime import FlywheelRuntime


class RunTriggerBody(BaseModel):
    phase: str = "daily"
    session_keys: list[str] | None = None
    auto_approve: bool | None = None


class ApprovalBody(BaseModel):
    approved_indices: list[int] | None = None


class RejectBody(BaseModel):
    reason: str = "用户在可视化画布中取消放行"


def build_runs_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    # ---- Sessions ----
    @router.get("/api/site/sessions")
    def site_list_sessions(status: str | None = None) -> dict[str, Any]:
        return {"sessions": [s.model_dump(mode="json") for s in rt.db.list_sessions(status=status, limit=100)]}

    @router.post("/api/site/sessions")
    def site_register_session(req: RegisterSessionRequest) -> dict[str, Any]:
        rec = rt.register_session(req)
        return {"session": rec.model_dump(mode="json")}

    # ---- P00-P07 Workflow Runs ----
    @router.get("/api/site/runs")
    def site_list_runs() -> dict[str, Any]:
        return {
            "runs": [r.model_dump(mode="json") for r in rt.db.list_workflow_runs(limit=30)],
            "lock": rt.db.get_workflow_lock(),
        }

    @router.get("/api/site/runs/{run_id}")
    def site_get_run(run_id: str) -> dict[str, Any]:
        run = rt.db.get_workflow_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return {
            "run": run.model_dump(mode="json"),
            "events": rt.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=50),
        }

    @router.get("/api/site/runs/{run_id}/events")
    def site_run_events(run_id: str) -> dict[str, Any]:
        return {"events": rt.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=50)}

    @router.post("/api/site/runs")
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

    @router.post("/api/site/runs/{run_id}/approve")
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

    @router.post("/api/site/runs/{run_id}/reject")
    def site_reject_run(run_id: str, body: RejectBody) -> dict[str, Any]:
        try:
            run = rt.orchestrator.reject_run(run_id, reason=body.reason)
            return {"run": run.model_dump(mode="json")}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
