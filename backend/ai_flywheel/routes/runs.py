"""Sessions and P00-P07 runs, including the background start / approval helpers."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai_flywheel.core.orchestrator import ApprovalLockBusy, WorkflowLockError
from ai_flywheel.models import DistillQueueItem, RegisterSessionRequest
from ai_flywheel.runtime import FlywheelRuntime


class RunTriggerBody(BaseModel):
    phase: str = "daily"
    session_keys: list[str] | None = None
    queue_ids: list[str] | None = None
    auto_approve: bool | None = None


class ApprovalBody(BaseModel):
    approved_indices: list[int] | None = None


class RejectBody(BaseModel):
    reason: str = "用户在可视化画布中取消放行"


class QueueActionBody(BaseModel):
    action: str
    reason: str = ""


class QueueBulkActionBody(BaseModel):
    queue_ids: list[str]
    action: str
    reason: str = ""


class QueueReTriageBody(BaseModel):
    dry_run: bool = True


#: The maintainer interventions the queue control plane accepts.
QUEUE_ITEM_ACTIONS = ("requeue", "exclude", "withdraw", "opt_out", "clear_opt_out")

#: The free-text fields a queue keyword search looks at.
QUEUE_SEARCH_FIELDS = (
    "title",
    "session_key",
    "project",
    "workspace",
    "exclude_reason_detail",
    "focus_note",
    "run_id",
)


def _normalize_queue_action(action: str) -> str:
    return str(action or "").strip().lower().replace("-", "_")


def _queue_item_matches(item: DistillQueueItem, needle: str) -> bool:
    haystack = " ".join(
        str(getattr(item, field) or "") for field in QUEUE_SEARCH_FIELDS
    ).lower()
    return needle in haystack


def build_runs_router(rt: FlywheelRuntime) -> APIRouter:
    router = APIRouter()

    def _queue_summary() -> dict[str, Any]:
        return rt.db.summarize_queue(
            max_sessions_per_run=rt.cfg.workflow.max_sessions_per_run
        )

    # ---- Sessions ----
    @router.get("/api/site/sessions")
    def site_list_sessions(status: str | None = None) -> dict[str, Any]:
        return {"sessions": [s.model_dump(mode="json") for s in rt.db.list_sessions(status=status, limit=100)]}

    @router.post("/api/site/sessions")
    def site_register_session(req: RegisterSessionRequest) -> dict[str, Any]:
        rec = rt.register_session(req)
        return {"session": rec.model_dump(mode="json")}

    # ---- Distill queue inventory & control plane ----
    @router.get("/api/site/queue")
    def site_list_queue(
        status: str | None = None,
        client: str | None = None,
        ingress_channel: str | None = None,
        project: str | None = None,
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        needle = (q or "").strip().lower()
        if needle:
            # The keyword filter spans several free-text columns: filtering a page
            # in SQL would count only that page, so the full candidate set is read
            # here and the count is exact.
            candidates = rt.db.list_queue_items(
                status=status,
                client=client,
                ingress_channel=ingress_channel,
                project=project,
                limit=5000,
            )
            matched = [item for item in candidates if _queue_item_matches(item, needle)]
            total = len(matched)
            page = matched[safe_offset : safe_offset + safe_limit]
        else:
            total = rt.db.count_queue_items(
                status=status,
                client=client,
                ingress_channel=ingress_channel,
                project=project,
            )
            page = rt.db.list_queue_items(
                status=status,
                client=client,
                ingress_channel=ingress_channel,
                project=project,
                limit=safe_limit,
                offset=safe_offset,
            )
        return {
            "summary": _queue_summary(),
            "items": [item.model_dump(mode="json") for item in page],
            "total": total,
        }

    @router.get("/api/site/queue/{queue_id}/events")
    def site_queue_events(queue_id: str) -> dict[str, Any]:
        return {
            "events": rt.db.list_audit_events(
                entity_type="queue_item", entity_id=queue_id, limit=50
            )
        }

    @router.post("/api/site/queue/sync")
    def site_queue_sync() -> dict[str, Any]:
        report = rt.queue.reconcile_codex_archives()
        bridged = rt.queue.bridge_pending_sessions_to_queue()
        return {"sync": report, "bridged": bridged, "summary": _queue_summary()}

    @router.post("/api/site/queue/re-triage")
    def site_queue_re_triage(body: QueueReTriageBody) -> dict[str, Any]:
        result = rt.queue.re_triage_queue(dry_run=body.dry_run, actor="portal")
        return {**result, "summary": _queue_summary()}

    @router.post("/api/site/queue/bulk-action")
    def site_queue_bulk_action(body: QueueBulkActionBody) -> dict[str, Any]:
        action = _normalize_queue_action(body.action)
        if action not in QUEUE_ITEM_ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"Unsupported queue action: {body.action}"
            )
        updated = 0
        for queue_id in dict.fromkeys(str(q) for q in body.queue_ids if str(q).strip()):
            try:
                item = rt.queue.apply_item_action(
                    queue_id, action, reason=body.reason, actor="portal"
                )
            except ValueError:
                # An opted-out session cannot be requeued: it is skipped, the
                # remaining ids still apply.
                continue
            if item is not None:
                updated += 1
        return {"updated": updated, "summary": _queue_summary()}

    @router.post("/api/site/queue/{queue_id}/action")
    def site_queue_action(queue_id: str, body: QueueActionBody) -> dict[str, Any]:
        action = _normalize_queue_action(body.action)
        if action not in QUEUE_ITEM_ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"Unsupported queue action: {body.action}"
            )
        try:
            item = rt.queue.apply_item_action(
                queue_id, action, reason=body.reason, actor="portal"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="Queue item not found")
        return {"item": item.model_dump(mode="json"), "summary": _queue_summary()}

    # ---- P00-P07 Workflow Runs ----
    @router.get("/api/site/runs")
    def site_list_runs(limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        runs = rt.db.list_workflow_runs(limit=safe_limit)
        return {
            "runs": [r.model_dump(mode="json") for r in runs],
            "total": rt.db.count_workflow_runs(),
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
                queue_ids=body.queue_ids,
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
                    queue_ids=body.queue_ids,
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
