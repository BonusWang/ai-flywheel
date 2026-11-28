"""The P00–P07 state machine, assembled from the pipeline package's stage modules.

This module owns the coordinator shell, node plumbing, approval lifecycle (claim,
resume, reject), and fail-run boundary. The stage implementations live in:

- :mod:`ai_flywheel.core.pipeline.stage_pre_approval` (P00–P03 + approval gate)
- :mod:`ai_flywheel.core.pipeline.stage_post_approval` (P04–P07)
- :mod:`ai_flywheel.core.pipeline.recovery` (heartbeat, lock checkpoints, terminal paths)
- :mod:`ai_flywheel.core.pipeline.helpers` (pure deterministic normalization & routing helpers)

Collaborators that tests and deployments replace on the public facade
(``ai_flywheel.core.orchestrator.build_runner`` and friends) are resolved through
that facade at call time, so an existing monkeypatch keeps taking effect.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.runners import (
    PlanDiagnostics,
    PlanUnavailableError,
    ReviewUnavailableError,
    resolve_runner_mode,
)
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.approval_channel import ApprovalChannel
from ai_flywheel.core.heartbeat import HeartbeatManager
from ai_flywheel.core.pipeline.constants import (
    NODE_TEMPLATES,
    REVIEW_SOURCE_LABELS,
    ApprovalLockBusy,
    WorkflowLockError,
    _error_payload_with_telemetry,
)
from ai_flywheel.core.pipeline.events import EventBroker
from ai_flywheel.core.pipeline.helpers import (
    HandDistilledPlan,
    filter_hand_distilled_plan,
    partial_write_payload,
)
from ai_flywheel.core.pipeline.recovery import RecoveryMixin
from ai_flywheel.core.pipeline.stage_post_approval import PostApprovalStagesMixin
from ai_flywheel.core.pipeline.stage_pre_approval import (
    PreApprovalStagesMixin,
    StageTracker,
    _empty_archive_sync_report,
    _facade_module,
)
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.queue_service import (
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_SKIP,
    DistillQueueService,
)
from ai_flywheel.models import (
    DistillQueueItem,
    INCIDENT_APPROVAL_REASON,
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    QueueStatus,
    RunStatus,
    SessionRecord,
    WorkflowRunRecord,
    is_incident_write,
    is_sensitive_write,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore

logger = logging.getLogger(__name__)


def resolve_approval_mode(cfg: FlywheelConfig, auto_approve_override: bool | None = None) -> str:
    """The approval mode a batch really runs with: ``smart``, ``always`` or ``auto``.

    The one answer every entry shares — the configured mode, the CLI flag, the portal's
    request and the background trigger all come through here, so none of them can run a
    batch under a mode another would have refused.

    ``None`` leaves the configured mode in force, read as ``effective_approval_mode``
    reads it. ``False`` asks for a human every time. ``True`` asks for ``auto``, and the
    safety valve has the last word on that: with ``safety_valves.auto_approve_writes``
    off the batch runs ``smart`` — it is held for a human where a human is needed, never
    waved through. Which is exactly what the configured ``auto`` comes to under the same
    valve.
    """
    if auto_approve_override is False:
        return "always"
    if auto_approve_override is True:
        return "auto" if cfg.safety_valves.auto_approve_writes else "smart"
    return cfg.effective_approval_mode



class WorkflowOrchestrator(RecoveryMixin, PreApprovalStagesMixin, PostApprovalStagesMixin):
    """Autonomous P00-P07 state machine with approval gate, fail-run safety, and SSE visibility."""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        fs_store: ImmutableFileStore,
        policies: PolicyEngine,
        activation: ActivationEngine,
        sessions: SessionAdapter,
        knowledge: KnowledgeSinkAdapter,
        delivery: DeliveryAdapter,
        broker: EventBroker,
        skill_writer: SkillWriterAdapter | None = None,
        heartbeat_interval_seconds: float = 15.0,
        feishu_channel: ApprovalChannel | None = None,
        on_node_begin: Callable[[str, str], None] | None = None,
        queue: DistillQueueService | None = None,
    ) -> None:
        self.feishu_channel: ApprovalChannel | None = feishu_channel
        self.cfg = cfg
        self.db = db
        self.fs_store = fs_store
        self.policies = policies
        self.activation = activation
        self.sessions = sessions
        self.knowledge = knowledge
        self.delivery = delivery
        self.broker = broker
        # The queue state machine the pipeline claims from and settles into. The
        # runtime passes its own instance; a directly constructed orchestrator (a
        # test, a script) gets an equivalent one so nothing has to be wired twice.
        self.queue = queue or DistillQueueService(cfg, db, sessions, broker=broker)
        self.skill_writer = skill_writer or SkillWriterAdapter(cfg)
        # Optional hook called right after a node starts: (run_id, node_id).
        # Verification harnesses use it to park a run mid-pipeline; production
        # never sets it.
        self.on_node_begin = on_node_begin
        self.heartbeat = HeartbeatManager(
            self.db.renew_workflow_lock,
            interval_seconds=heartbeat_interval_seconds,
        )

    # ---------------- node plumbing & approval policy ----------------

    @staticmethod
    def resolve_phase(phase: str, now_utc: datetime | None = None) -> str:
        """Resolve 'auto' using Asia/Shanghai (UTC+8): Friday -> 'both' (daily + weekly review), else 'daily'."""
        if phase != "auto":
            return phase
        dt = now_utc or datetime.now(timezone.utc)
        shanghai = dt.astimezone(timezone(timedelta(hours=8)))
        return "both" if shanghai.weekday() == 4 else "daily"

    @staticmethod
    def _needs_manual_approval(
        planned_writes: list[PlannedWrite],
        approval_mode: str,
        high_write_threshold: int = 6,
    ) -> tuple[bool, list[str]]:
        """Whether a batch stops for a human before anything is written, and the reasons it shows.

        ``always`` stops for every batch and ``smart`` for the sensitive ones. ``auto``
        releases routine writes without asking — and nothing else: an incident card is a
        record of what went wrong and why, and it is held for a human under ``auto`` as it
        is under every other mode. An approval mode is not a licence to write one unseen.
        """
        if not planned_writes:
            return False, []
        if approval_mode == "always":
            return True, [f"当前为每次人工审批模式（共 {len(planned_writes)} 项）"]
        if approval_mode == "auto":
            forced = [
                write.approval_reason or INCIDENT_APPROVAL_REASON
                for write in planned_writes
                if is_incident_write(write)
            ]
            return bool(forced), forced
        # Smart mode: routine notes & trial rules auto-pass; only Skill creation/updates, high-risk, or large batches pause
        reasons: list[str] = []
        for w in planned_writes:
            # The same predicate the Feishu card and the portal use: a write the
            # card calls sensitive is sensitive for the gate, too.
            if not is_sensitive_write(w):
                continue
            if w.target_kind == "skill_improvement":
                reasons.append(w.approval_reason or f"涉及 Skill 变更：{w.title}")
            else:
                reasons.append(w.approval_reason or f"重要变更：{w.title}")
        if len(planned_writes) > high_write_threshold:
            reasons.append(f"本批建议写入共 {len(planned_writes)} 项（超过自动放行阈值 {high_write_threshold}）")
        return bool(reasons), reasons

    def _new_nodes(self) -> list[NodeExecution]:
        return [
            NodeExecution(node_id=nid, name=name, actor=actor, status=NodeStatus.PENDING)
            for nid, name, actor in NODE_TEMPLATES
        ]

    def _get_node(self, run: WorkflowRunRecord, node_id: str) -> NodeExecution:
        for n in run.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)

    # ---------------- queue plumbing ----------------

    def _claim_queue_item(self, item: DistillQueueItem, run: WorkflowRunRecord) -> DistillQueueItem | None:
        """Bind one queued item to this run as PROCESSING (the P01 claim)."""
        return self.db.update_queue_item_status(
            item.queue_id,
            QueueStatus.PROCESSING,
            run_id=run.run_id,
            actor="p01_begin",
            reason_code="RUN_CLAIMED",
            summary=f"claimed by {run.run_id}",
        )

    def _publish_queue_update(self, source: str, run_id: str, queue_ids: list[str]) -> None:
        """Tell the portal the inventory of this run changed (one SSE event per stage)."""
        if not queue_ids:
            return
        self.broker.publish(
            "queue_updated",
            {"source": source, "run_id": run_id, "queue_ids": list(queue_ids)},
        )

    def _claimed_queue_ids(self, run: WorkflowRunRecord) -> list[str]:
        """The ids P01 claimed for this run, read from the node payload."""
        try:
            payload = self._get_node(run, "P01_BEGIN").payload
        except KeyError:
            return []
        if not isinstance(payload, dict):
            return []
        return [str(qid) for qid in (payload.get("claimed_queue_ids") or [])]

    def _emit_run(self, event_type: str, run: WorkflowRunRecord, extra: dict[str, Any] | None = None) -> None:
        data = {
            "run_id": run.run_id,
            "status": run.status.value,
            "lock_released": run.lock_released,
            "nodes": [n.model_dump(mode="json") for n in run.nodes],
            "planned_writes": [w.model_dump(mode="json") for w in run.planned_writes],
        }
        if extra:
            data.update(extra)
        self.broker.publish(event_type, data)

    def _begin_node(self, run: WorkflowRunRecord, node_id: str) -> float:
        node = self._get_node(run, node_id)
        node.status = NodeStatus.RUNNING
        node.started_at = utc_now_iso()
        self.db.save_workflow_run(run)
        if self.on_node_begin is not None:
            self.on_node_begin(run.run_id, node_id)
        self._emit_run("node_update", run, {"active_node": node_id})
        return time.perf_counter()

    def _finish_node(
        self,
        run: WorkflowRunRecord,
        node_id: str,
        t0: float,
        *,
        status: NodeStatus = NodeStatus.PASS,
        summary: str = "",
        internal_checks: list[dict[str, Any]] | None = None,
        artifacts: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        node = self._get_node(run, node_id)
        node.status = status
        node.finished_at = utc_now_iso()
        node.duration_ms = max(1, int((time.perf_counter() - t0) * 1000))
        node.summary = summary
        if internal_checks is not None:
            node.internal_checks = internal_checks
        if artifacts is not None:
            node.artifacts = artifacts
        if payload is not None:
            node.payload = payload
        _, rhash = self.fs_store.write_receipt(
            f"runs/{run.run_id}",
            node_id,
            node.model_dump(mode="json"),
        )
        node.receipt_hash = rhash[:16]
        self.db.save_workflow_run(run)
        # Node-end lock renewal: non-fatal here; the next synchronous checkpoint
        # (or the heartbeat watchdog semantics) treats actual loss as a fail-run.
        try:
            self.db.renew_workflow_lock(run.run_id)
        except Exception:
            logger.warning("lock renewal at node end failed for %s", run.run_id, exc_info=True)
        self._emit_run("node_update", run, {"active_node": node_id})

    def _trace_dropped_plan_items(self, run: WorkflowRunRecord, plan_info: PlanDiagnostics) -> None:
        """Put every planned-write item that did not make it into the plan into the audit trail and the log."""
        invalid = len(plan_info.dropped_items) - plan_info.over_limit_count
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run.run_id,
            event_type="plan_items_dropped",
            actor="p03_plan",
            summary=(
                f"{len(plan_info.dropped_items)} 个计划项未列入写入计划"
                f"（{invalid} 项未通过校验，{plan_info.over_limit_count} 项超出单批上限）"
            ),
            payload=plan_info.to_dict(),
        )
        logger.warning(
            "Run %s: %s planned-write item(s) not planned (%s invalid, %s over limit): %s",
            run.run_id,
            len(plan_info.dropped_items),
            invalid,
            plan_info.over_limit_count,
            [(item["index"], item["reasons"]) for item in plan_info.dropped_items],
        )

    def _trace_unavailable_plan(self, run: WorkflowRunRecord, exc: PlanUnavailableError) -> None:
        """Leave the per-item evidence on the P03 node and in the audit trail before the run fails."""
        self._get_node(run, "P03_ROUTE_PLAN").payload = {
            "plan_source": exc.diagnostics.source,
            "plan_diagnostics": exc.diagnostics.to_dict(),
            "plan_error": str(exc),
        }
        if exc.diagnostics.dropped_items:
            self._trace_dropped_plan_items(run, exc.diagnostics)
        logger.warning("Run %s: P03 write plan unavailable: %s", run.run_id, exc)

    def _trace_unavailable_review(self, run: WorkflowRunRecord, exc: ReviewUnavailableError) -> None:
        """Leave the reason on the P02 node and in the audit trail before the run fails."""
        diagnostics = exc.diagnostics
        self._get_node(run, "P02_REVIEW").payload = {
            "review_source": diagnostics.source,
            "review_diagnostics": diagnostics.to_dict(),
            "review_error": str(exc),
        }
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run.run_id,
            event_type="review_unavailable",
            actor="p02_review",
            summary=(
                f"{REVIEW_SOURCE_LABELS.get(diagnostics.source, diagnostics.source)}的审查结果不可用，本批不审查、不写入"
            ),
            payload={**diagnostics.to_dict(), "message": str(exc)},
        )
        logger.warning("Run %s: P02 review unavailable: %s", run.run_id, exc)

    def _stored_sessions(self, session_keys: list[str]) -> dict[str, SessionRecord]:
        """Source session records of a planned write, looked up where runs persist them."""
        return {record.session_key: record for key in session_keys if (record := self.db.get_session(key))}

    def _distill_modes(self, session_keys: list[str]) -> dict[str, str]:
        """How each of these sessions is distilled right now (``full`` unless declared otherwise)."""
        modes: dict[str, str] = {}
        for key in dict.fromkeys(session_keys):
            record = self.db.get_session(key)
            if record is not None:
                modes[key] = self.queue.distill_mode_of(record)
        return modes

    def _declared_keys(self, session_keys: list[str], modes: frozenset[str]) -> list[str]:
        """The sessions currently distilled in one of ``modes``.

        Always read from the database, never carried over from an earlier node: a
        client may declare a session hand-distilled while a batch that admitted it
        is still between P01 and P04.
        """
        current = self._distill_modes(session_keys)
        return [key for key, mode in current.items() if mode in modes]

    # ---------------- sessions the client hand-distilled ----------------

    def _taken_out_of_distillation(self, session_key: str) -> bool:
        """Whether the session is declared ``hand_distilled`` with ``governance_only=false`` right now.

        Read from the database on every call: the declaration can be made at any moment,
        including while a batch that named this session is already between P00 and P01.
        Such a session produces nothing — no document, no rule, no Skill, no mistake or
        dissent — so it is not admitted in the first place. Only an explicit enqueue,
        which makes a new declaration, takes it back.
        """
        return self._distill_modes([session_key]).get(session_key) == DISTILL_MODE_SKIPPED

    def _leave_out_hand_distilled(
        self,
        run: WorkflowRunRecord,
        items: list[PlannedWrite],
        *,
        actor: str,
        documents_summary: str,
        items_summary: str,
        report_unknown_keys: bool = True,
    ) -> tuple[HandDistilledPlan, set[str]]:
        """Take what may not be written out of ``items``, by the declarations in the database.

        Every declaration is read from the database here and nowhere else — not from P01
        or P02: a client may declare a session hand-distilled while a batch that admitted
        it is waiting for approval. A document whose knowledge was written by hand is left
        out; so is an item of any kind behind a session taken out of distillation, or of a
        batch that admitted no session. What was left out goes to the audit trail. This
        never fails a node: a plan left with nothing to write is an empty plan, not an
        unavailable one.

        Returns the filtered plan and the sessions currently taken out of distillation,
        for the candidates P02 found.
        """
        cited = [str(key) for item in items for key in item.source_session_keys]
        modes = self._distill_modes([*run.admitted_sessions, *cited])
        governance = {key for key, mode in modes.items() if mode == DISTILL_MODE_GOVERNANCE_ONLY}
        taken_out = {key for key, mode in modes.items() if mode == DISTILL_MODE_SKIPPED}
        plan = filter_hand_distilled_plan(
            items,
            governance_only_keys=governance,
            taken_out_keys=taken_out,
            admitted_keys=run.admitted_sessions,
        )
        if plan.documents_left_out:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="governance_only_wiki_skipped",
                actor=actor,
                summary=f"{len(plan.documents_left_out)} {documents_summary}",
                payload={
                    "skipped_docs": plan.documents_left_out,
                    "governance_only_session_keys": sorted(governance | taken_out),
                },
            )
        if plan.items_left_out:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="plan_items_left_out",
                actor=actor,
                summary=f"{len(plan.items_left_out)} {items_summary}",
                payload={
                    "items": plan.items_left_out,
                    "taken_out_session_keys": sorted(taken_out),
                    "admitted_session_keys": list(run.admitted_sessions),
                },
            )
        if report_unknown_keys and plan.unknown_source_keys:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="plan_item_unknown_source_keys",
                actor=actor,
                summary=(
                    f"{len(plan.unknown_source_keys)} 项计划引用了不在本批送审会话中的来源键，"
                    "已剥离，不作为来源采信"
                ),
                payload={"items": plan.unknown_source_keys, "admitted_session_keys": list(run.admitted_sessions)},
            )
        return plan, taken_out

    @staticmethod
    def _reviewed_session(sess: SessionRecord, item: DistillQueueItem) -> SessionRecord:
        """The session as P02 reviews it: carrying the structured brief its queue item holds.

        A client may submit a structured brief with a session: the tables, configuration,
        root causes and decisions it made sure of. The queue keeps it on the first chunk
        of the item. It is attached here to the session record the review reads, so the
        reviewer puts it on top of the dialogue and carries it whole, whatever became of
        the text (its size, a compaction, a caller that trims it). Nothing is persisted:
        the copy lives as long as the batch, and a record that already carries a brief
        is returned as it is.
        """
        payload = item.payload if isinstance(item.payload, dict) else {}
        brief = payload.get("structured_brief") if item.chunk_index <= 1 else None
        if not brief or not isinstance(sess.metadata, dict) or sess.metadata.get("structured_brief"):
            return sess
        return sess.model_copy(update={"metadata": {**sess.metadata, "structured_brief": brief}})

    def _record_partial_writes(
        self,
        run: WorkflowRunRecord,
        write_receipts: list[dict[str, Any]],
        written_paths: list[str],
        error: BaseException,
    ) -> None:
        """Leave the receipts of a half-done batch on the P04 node before the run fails."""
        node = self._get_node(run, "P04_WRITE_VERIFY")
        node.payload = partial_write_payload(node.payload, write_receipts, written_paths, error)

    # ---------------- the run: P00 through P03 and the approval gate ----------------

    @staticmethod
    def new_run_id() -> str:
        """The id of a run that has not been created yet."""
        return f"run-{uuid.uuid4().hex[:10]}"


    def _enter_stage(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        node_id: str,
        simulate_fail_node: str | None,
        fail_message: str,
    ) -> float:
        tracker.active_node = node_id
        t0 = self._begin_node(run, node_id)
        tracker.t0 = t0
        if simulate_fail_node == node_id:
            raise RuntimeError(fail_message)
        return t0

    def start_run(
        self,
        *,
        trigger: str = "manual",
        phase: str = "daily",
        session_keys: list[str] | None = None,
        queue_ids: list[str] | None = None,
        auto_approve_override: bool | None = None,
        simulate_fail_node: str | None = None,
        run_id: str | None = None,
    ) -> WorkflowRunRecord:
        if not self.cfg.safety_valves.workflow_enabled:
            raise RuntimeError("Workflow execution is disabled by safety valve WORKFLOW_ENABLED=false.")

        # A caller that already holds the lock for this id (the background
        # trigger takes the lock before handing the pipeline to a thread) may
        # pass the id in; taking the lock again for the same id just refreshes it.
        run_id = run_id or self.new_run_id()
        if not self.db.acquire_workflow_lock(run_id):
            lock = self.db.get_workflow_lock()
            raise WorkflowLockError(f"Another workflow run already holds the lock: {lock}")
        self._start_heartbeat(run_id)

        runner_mode = resolve_runner_mode(self.cfg)
        resolved_phase = self.resolve_phase(phase)
        if resolved_phase == "both" and not self.cfg.workflow.friday_weekly_enabled:
            resolved_phase = "daily"
        # The one reading of the approval mode, for this record, the gate below and every entry.
        init_approval_mode = resolve_approval_mode(self.cfg, auto_approve_override)
        run = WorkflowRunRecord(
            run_id=run_id,
            trigger=trigger,
            phase=resolved_phase,
            runner_mode=runner_mode,
            approval_mode=init_approval_mode,
            status=RunStatus.RUNNING,
            nodes=self._new_nodes(),
            lock_released=False,
        )
        self.db.save_workflow_run(run)
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run_id,
            event_type="run_started",
            actor=trigger,
            summary=f"启动批次（阶段={resolved_phase}，审批模式={init_approval_mode}）",
            payload={"phase": resolved_phase, "approval_mode": init_approval_mode, "runner": runner_mode},
        )
        if auto_approve_override is True and init_approval_mode != "auto":
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run_id,
                event_type="auto_approve_vetoed",
                actor="safety_valve",
                summary=(
                    "请求自动放行被安全阀否决（auto_approve_writes=false），"
                    "本批按 smart 审批：敏感写入与事故卡仍需人工确认"
                ),
                payload={"requested": "auto", "effective": init_approval_mode},
            )
        self._emit_run("run_started", run)

        tracker = StageTracker(active_node="P00_PREPARE", t0=time.perf_counter())
        try:
            selected_sessions = self._run_p00_prepare(
                run,
                tracker,
                session_keys=session_keys,
                queue_ids=queue_ids,
                simulate_fail_node=simulate_fail_node,
            )
            admitted = self._run_p01_begin(
                run,
                tracker,
                selected_sessions=selected_sessions,
                session_keys=session_keys,
                queue_ids=queue_ids,
                runner_mode=runner_mode,
                simulate_fail_node=simulate_fail_node,
            )
            runner, existing_skills, review_findings = self._run_p02_review(
                run,
                tracker,
                admitted=admitted,
                resolved_phase=resolved_phase,
                simulate_fail_node=simulate_fail_node,
            )
            planned_writes = self._run_p03_route_plan(
                run,
                tracker,
                admitted=admitted,
                runner=runner,
                existing_skills=existing_skills,
                review_findings=review_findings,
                resolved_phase=resolved_phase,
                simulate_fail_node=simulate_fail_node,
            )
            if self._handle_approval_gate(run, planned_writes, init_approval_mode):
                return run
            return self._execute_post_approval(run, simulate_fail_node=simulate_fail_node)
        except Exception as exc:
            return self._fail_run(
                run, tracker.active_node, str(exc), tracker.t0, extra_payload=_error_payload_with_telemetry(exc)
            )
        finally:
            self._stop_heartbeat()

    # ---------------- approval: claim, resume, reject ----------------

    def approve_and_resume(
        self,
        run_id: str,
        approved_indices: list[int] | None = None,
        simulate_fail_node: str | None = None,
        *,
        source: str = "portal",
        selected_option: str = "同意并写入",
    ) -> WorkflowRunRecord:
        run = self.claim_approval_for_resume(
            run_id, approved_indices=approved_indices, source=source, selected_option=selected_option
        )
        return self.resume_claimed_run(run, simulate_fail_node=simulate_fail_node)

    def claim_approval_for_resume(
        self,
        run_id: str,
        approved_indices: list[int] | None = None,
        *,
        source: str = "portal",
        selected_option: str = "同意并写入",
    ) -> WorkflowRunRecord:
        """The fast half of an approval: validate, claim atomically, return the fresh run.

        The claim is one transaction and one lock acquisition; the pipeline that
        follows is slow, so a caller that wants to answer the HTTP request
        immediately (the background trigger) can claim first and resume on a
        worker thread.
        """
        run = self.db.get_workflow_run(run_id)
        if not run:
            raise KeyError(f"Workflow run not found: {run_id}")
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError(f"Run {run_id} is in status {run.status.value}, not AWAITING_APPROVAL.")

        if approved_indices is not None:
            keep = set(approved_indices)
            mask = [idx in keep for idx in range(len(run.planned_writes))]
        else:
            mask = [True] * len(run.planned_writes)

        res = self.db.claim_approval(
            run_id, approved_mask=mask, selected_option=selected_option, source=source
        )
        if not res["claimed"]:
            if res["reason"] == "lock_busy":
                raise ApprovalLockBusy(
                    f"另一个运行 {res['lock_run_id']} 持有独占锁，预计 {res['lock_expires_at']} 后可重试",
                    lock_run_id=res["lock_run_id"],
                    lock_expires_at=res["lock_expires_at"],
                )
            raise ValueError(f"Run {run_id} is no longer awaiting approval.")

        # The claim transaction's writes are the single source of truth.
        return self.db.get_workflow_run(run_id) or run

    def resume_claimed_run(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        """The slow half of an approval: the post-approval pipeline, under the heartbeat."""
        self._start_heartbeat(run.run_id)
        try:
            return self._execute_post_approval(run, simulate_fail_node=simulate_fail_node)
        finally:
            self._stop_heartbeat()

    def reject_run(
        self,
        run_id: str,
        reason: str = "用户取消放行",
        *,
        source: str = "portal",
    ) -> WorkflowRunRecord:
        run = self.db.get_workflow_run(run_id)
        if not run:
            raise KeyError(f"Workflow run not found: {run_id}")
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError(f"Run {run_id} is not awaiting approval.")

        self._mark_remaining_nodes_skipped(
            run,
            node_ids=("P04_WRITE_VERIFY", "P05_SETTLE", "P06_REPORT", "P07_DELIVER"),
            reason=f"审核拒绝放行：{reason}",
        )
        nodes_json = json.dumps([n.model_dump(mode="json") for n in run.nodes], ensure_ascii=False)
        digest_markdown = (
            f"## AI Flywheel 运行已取消放行（{run.run_id}）\n\n"
            f"- 原因：{reason}\n- 已安全释放锁并将入队会话恢复为 `pending`。\n"
        )
        ok = self.db.claim_rejection(
            run_id,
            reason=reason,
            source=source,
            nodes_json=nodes_json,
            digest_markdown=digest_markdown,
        )
        if not ok:
            raise ValueError(f"Run {run_id} is no longer awaiting approval.")

        # A cancelled batch is not a failure: every claimed queue item returns to
        # `queued` untouched, without a retry penalty.
        self._release_queue_items_for_run(
            run_id,
            error="",
            increment_retry=False,
            actor="approval_gate",
            reason_code="APPROVAL_REJECTED",
        )

        fresh = self.db.get_workflow_run(run_id) or run
        # The claim transaction already wrote the terminal row, released the lock
        # and put the admitted sessions back to pending; only the receipt and the
        # event are left.
        return self._finalize_terminal_artifacts(
            fresh,
            status=RunStatus.CANCELLED,
            error=reason,
            receipt_name=f"{run_id}-cancelled",
            event_type="run_rejected",
            extra={"reason": reason},
            release_sessions=False,
            save_row=False,
        )

    # ---------------- P04 through P07 ----------------

    def _approval_source_for(self, run: WorkflowRunRecord) -> str:
        """Who approved this run, taken from the claim transaction's audit event.

        A run claimed through the portal or Feishu carries exactly one ``approved``
        event naming its source; a batch that never paused has none and is its own
        approval.
        """
        for row in self.db.list_audit_events(
            entity_type="workflow_run", entity_id=run.run_id, limit=50
        ):
            if row.get("event_type") != "approved":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            return str(payload.get("source") or row.get("actor") or "portal")
        return "auto"


    def _execute_post_approval(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        tracker = StageTracker(active_node="P04_WRITE_VERIFY", t0=time.perf_counter())
        try:
            p04 = self._run_p04_write_verify(run, tracker, simulate_fail_node=simulate_fail_node)
            p05 = self._run_p05_settle(
                run,
                tracker,
                created_rules=p04.created_rules,
                simulate_fail_node=simulate_fail_node,
            )
            self._run_p06_report(run, tracker, p04=p04, p05=p05, simulate_fail_node=simulate_fail_node)
            return self._run_p07_deliver(run, tracker, p04=p04, simulate_fail_node=simulate_fail_node)
        except Exception as exc:
            return self._fail_run(
                run, tracker.active_node, str(exc), tracker.t0, extra_payload=_error_payload_with_telemetry(exc)
            )
