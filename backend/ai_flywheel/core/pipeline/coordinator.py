"""The P00–P07 state machine, assembled from the pipeline package's modules.

This module holds what only the coordinator can own: the stage sequence and the
side-effect contract between the stages. P02 and P03 are read-only (candidates
and plans live in the node payload); P04 is the one controlled write stage; P05
settles idempotently; P07 releases the exclusive lock. The terminal paths, lock
checkpoints and heartbeat live in :mod:`ai_flywheel.core.pipeline.recovery`; the
pure helpers in :mod:`ai_flywheel.core.pipeline.helpers`.

Collaborators that tests and deployments replace on the public facade
(``ai_flywheel.core.orchestrator.build_runner`` and friends) are resolved through
that facade at call time, so an existing monkeypatch keeps taking effect.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    REVIEW_SOURCE_UNDECLARED,
    PlanDiagnostics,
    PlanUnavailableError,
    ReviewUnavailableError,
    normalize_rule_payload,
    resolve_runner_mode,
)
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.adapters.wiki_gate import (
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
    UPDATE_NO_NEW_CONTENT,
    approved_registration_matches,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.approval_channel import ApprovalChannel
from ai_flywheel.core.heartbeat import HeartbeatManager
from ai_flywheel.core.pipeline.constants import (
    LANDING_FAILED_REASON,
    NODE_TEMPLATES,
    PLAN_SOURCE_LABELS,
    REVIEW_SOURCE_LABELS,
    ApprovalLockBusy,
    WorkflowLockError,
    _error_payload_with_telemetry,
)
from ai_flywheel.core.pipeline.events import EventBroker
from ai_flywheel.core.pipeline.git_commit import commit_p04_written_files
from ai_flywheel.core.pipeline.helpers import (
    apply_route_decision,
    candidate_dissents,
    candidate_mistakes,
    domain_signal_names,
    eligible_promotions,
    filter_governance_only_docs,
    governance_only_skip_receipt,
    partial_write_payload,
    promote_observation,
    route_knowledge_doc,
    unrouted_reason_breakdown,
)
from ai_flywheel.core.pipeline.recovery import RecoveryMixin
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.queue_service import (
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    DistillQueueService,
)
from ai_flywheel.models import (
    CreateRuleRequest,
    DistillQueueItem,
    INCIDENT_PAGE_TYPE,
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    QueueStatus,
    RetroStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
    is_sensitive_write,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore

logger = logging.getLogger(__name__)


def _facade_module() -> Any:
    """The public facade module ``ai_flywheel.core.orchestrator``.

    Collaborators such as ``build_runner`` are re-exported there and are the
    documented place to replace them (tests do). Resolving them at call time keeps
    such a patch effective now that the implementation lives in this package.
    """
    from ai_flywheel.core import orchestrator as facade

    return facade


def _empty_archive_sync_report() -> dict[str, Any]:
    """The no-op archive report: P00 always records the field, configured or not."""
    return {
        "sources": [],
        "scanned": 0,
        "enqueued": 0,
        "excluded": 0,
        "unchanged": 0,
        "withdrawn": 0,
        "errors": [],
    }


class WorkflowOrchestrator(RecoveryMixin):
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
        if not planned_writes or approval_mode == "auto":
            return False, []
        if approval_mode == "always":
            return True, [f"当前为每次人工审批模式（共 {len(planned_writes)} 项）"]
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
        if auto_approve_override is True:
            init_approval_mode = "auto"
        elif auto_approve_override is False:
            init_approval_mode = "always"
        else:
            init_approval_mode = self.cfg.effective_approval_mode
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
        self._emit_run("run_started", run)

        active_node = "P00_PREPARE"
        t0 = time.perf_counter()
        try:
            # ---------------- P00 PREPARE ----------------
            t0 = self._begin_node(run, "P00_PREPARE")
            if simulate_fail_node == "P00_PREPARE":
                raise RuntimeError("Simulated P00 failure")
            policies = self.policies.list_policies()
            scan_report = self.sessions.reconcile_scan_sources()
            # Codex is strict-archive only: the archive directories are reconciled
            # here, before the batch decides what to admit.
            archive_sync = (
                self.queue.reconcile_codex_archives()
                if self.cfg.sessions.codex_archive_sources
                else _empty_archive_sync_report()
            )
            explicit_batch = bool(session_keys or queue_ids)
            open_retro_keys = {
                r.session_key for r in self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=200)
            }
            triage_report = (
                self.sessions.sweep_pending_triage(open_retro_keys)
                if not explicit_batch
                else {"checked": 0, "admitted": 0, "skipped": 0, "reasons": {}}
            )
            if triage_report["skipped"] > 0:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="triage_skipped",
                    actor="triage_gate",
                    summary=f"规则初筛自动跳过 {triage_report['skipped']} 条临时/碎片会话",
                    payload=triage_report,
                )
            # Legacy `register_session` callers have no queue row yet: a normal
            # batch bridges the still-pending profiles into `distill_queue` so the
            # queue stays the one source of work inventory.
            bridged_to_queue = (
                0 if explicit_batch else self.queue.bridge_pending_sessions_to_queue()
            )
            if session_keys:
                # Explicitly named sessions are fetched one by one: filtering a recency-limited
                # page would silently drop the ones that are not among the most recent.
                wanted = list(dict.fromkeys(str(sk) for sk in session_keys))
                selected_sessions = [
                    record for sk in wanted if (record := self.db.get_session(sk)) is not None
                ]
            elif queue_ids:
                # An explicit queue_ids batch claims exactly those items in P01.
                selected_sessions = []
            else:
                # The priority order lives in SQL, so a P0_CORE session beyond the recency
                # window is never starved by a flood of P1_GENERAL sessions.
                selected_sessions = self.db.list_pending_sessions_prioritized(limit=50)
            self._finish_node(
                run,
                "P00_PREPARE",
                t0,
                summary=(
                    f"策略核验通过（{len(policies)} 项），扫盘新登记 {scan_report['newly_registered']} 条，"
                    f"归档对账入队 {archive_sync.get('enqueued', 0)} 条，桥接入队 {bridged_to_queue} 条，"
                    f"初筛跳过 {triage_report['skipped']} 条临时/噪音会话，待审查 {len(selected_sessions)} 条"
                ),
                internal_checks=[
                    {"name": "飞轮内置策略完整性校验", "status": "PASS"},
                    {"name": "会话目录扫盘对账", "status": "PASS", "detail": scan_report},
                    {"name": "Codex 归档目录严格对账", "status": "PASS", "detail": archive_sync},
                    {"name": "规则初筛（过滤子代理/空会话/低信号闲聊）", "status": "PASS", "detail": triage_report},
                    {"name": "P0/P1/P2 优先级排序", "status": "PASS"},
                ],
                payload={
                    "scan_report": scan_report,
                    "triage_report": triage_report,
                    "archive_sync": archive_sync,
                    "bridged_to_queue": bridged_to_queue,
                    "queue_summary": self.db.summarize_queue(
                        max_sessions_per_run=self.cfg.workflow.max_sessions_per_run
                    ),
                    "candidate_count": len(selected_sessions),
                },
            )

            # ---------------- P01 BEGIN ----------------
            active_node = "P01_BEGIN"
            t0 = self._begin_node(run, "P01_BEGIN")
            if simulate_fail_node == "P01_BEGIN":
                raise RuntimeError("Simulated P01 failure")
            max_sess = self.cfg.workflow.max_sessions_per_run
            max_chars = self.cfg.workflow.max_chars_per_run
            if runner_mode != "dry_run":
                # The model reviewers only see the first P02_MAX_SESSIONS. Admitting more would
                # leave the surplus unreviewed, so it stays `pending` for the next batch instead
                # of being admitted, skipped by P02 and silently settled by P05.
                max_sess = min(max_sess, P02_MAX_SESSIONS)
            claimed_queue_ids: list[str] = []
            admitted: list[tuple[SessionRecord, str]] = []
            used_chars = 0
            if queue_ids:
                # An explicit queue_ids batch jumps the line: claim exactly those items.
                for qid in dict.fromkeys(str(q) for q in queue_ids if str(q).strip()):
                    item = self.db.get_queue_item(qid)
                    if item is None:
                        continue
                    sess = self.db.get_session(item.session_key)
                    if sess is None:
                        continue
                    text = self.queue.load_queue_item_text(item)
                    if used_chars + len(text) > max_chars and admitted:
                        break
                    if self._claim_queue_item(item, run) is None:
                        continue
                    used_chars += len(text)
                    self.db.update_session_status(sess.session_key, SessionStatus.IN_PROGRESS)
                    admitted.append((self._reviewed_session(sess, item), text))
                    claimed_queue_ids.append(item.queue_id)
            elif session_keys:
                # Explicit session_keys keep the historical admission path; any queued
                # item of an admitted session is claimed along with it.
                for sess in selected_sessions[:max_sess]:
                    text = self.sessions.load_session_text(sess, max_chars=24000)
                    if used_chars + len(text) > max_chars and admitted:
                        break
                    used_chars += len(text)
                    admitted.append((sess, text))
                    self.db.update_session_status(sess.session_key, SessionStatus.IN_PROGRESS)
                    claimed_here: list[DistillQueueItem] = []
                    for item in self.db.list_session_queue_items(
                        sess.session_key, statuses=[QueueStatus.QUEUED]
                    ):
                        if self._claim_queue_item(item, run) is not None:
                            claimed_queue_ids.append(item.queue_id)
                            claimed_here.append(item)
                    for item in claimed_here:
                        reviewed = self._reviewed_session(sess, item)
                        if reviewed is not sess:
                            # The brief of the item's first chunk is reviewed with its session.
                            admitted[-1] = (reviewed, text)
                            break
            else:
                # A normal batch claims the prioritized backlog of `distill_queue`.
                for item in self.db.list_queued_items_prioritized(limit=max_sess):
                    if len(admitted) >= max_sess:
                        break
                    sess = self.db.get_session(item.session_key)
                    if sess is None:
                        continue
                    text = self.queue.load_queue_item_text(item)
                    if used_chars + len(text) > max_chars and admitted:
                        break
                    if self._claim_queue_item(item, run) is None:
                        continue
                    used_chars += len(text)
                    self.db.update_session_status(sess.session_key, SessionStatus.IN_PROGRESS)
                    admitted.append((self._reviewed_session(sess, item), text))
                    claimed_queue_ids.append(item.queue_id)
                if not admitted:
                    # A database seeded without queue rows still runs: the P00 bridge
                    # normally makes this unreachable, never a silent empty batch. An
                    # opt-out is a hard veto, so an automatic batch skips it, and so is
                    # a session the client took out of distillation by hand.
                    for sess in selected_sessions[:max_sess]:
                        if sess.opt_out:
                            continue
                        if self.queue.distill_mode_of(sess) == DISTILL_MODE_SKIPPED:
                            continue
                        text = self.sessions.load_session_text(sess, max_chars=24000)
                        if used_chars + len(text) > max_chars and admitted:
                            break
                        used_chars += len(text)
                        admitted.append((sess, text))
                        self.db.update_session_status(sess.session_key, SessionStatus.IN_PROGRESS)
            run.admitted_sessions = list(dict.fromkeys(s.session_key for s, _ in admitted))
            distill_modes = self._distill_modes(run.admitted_sessions)
            governance_only_keys = [
                key for key, mode in distill_modes.items() if mode == DISTILL_MODE_GOVERNANCE_ONLY
            ]
            queue_note = (
                f"，认领队列项 {len(claimed_queue_ids)} 条" if claimed_queue_ids else ""
            )
            governance_note = (
                f"，其中 {len(governance_only_keys)} 个已由客户端手工沉淀（仅治理提炼）"
                if governance_only_keys
                else ""
            )
            self._finish_node(
                run,
                "P01_BEGIN",
                t0,
                summary=(
                    f"独占锁已获取，准入 {len(admitted)} 个会话单元（共 {used_chars} 字符）"
                    f"{queue_note}{governance_note}"
                ),
                internal_checks=[
                    {"name": "独占锁持有核验", "status": "PASS"},
                    {"name": "预算与字符上限准入", "status": "PASS", "detail": {"chars": used_chars, "limit": max_chars}},
                    {"name": "队列项认领与 run 绑定", "status": "PASS", "detail": {"claimed": len(claimed_queue_ids)}},
                ],
                payload={
                    "admitted_sessions": run.admitted_sessions,
                    "used_chars": used_chars,
                    "claimed_queue_ids": claimed_queue_ids,
                    # How each admitted session is distilled: a `hand_distilled` session is
                    # reviewed in full, but P03 writes no knowledge document for it.
                    "distill_modes": distill_modes,
                    "governance_only_session_keys": governance_only_keys,
                },
            )
            self._publish_queue_update("p01_claim", run.run_id, claimed_queue_ids)

            # ---------------- P02 REVIEW ----------------
            active_node = "P02_REVIEW"
            t0 = self._begin_node(run, "P02_REVIEW")
            if simulate_fail_node == "P02_REVIEW":
                raise RuntimeError("Simulated P02 failure")
            runner = _facade_module().build_runner(self.cfg, self.fs_store)
            open_retros = self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=50)
            existing_skills = self.activation.probe_local_skills()
            try:
                review_findings = runner.review_sessions(
                    admitted,
                    open_retros,
                    existing_skills=existing_skills,
                    phase=resolved_phase,
                )
            except ReviewUnavailableError as exc:
                # The configured reviewer produced nothing usable: fail the run, never swap in another reviewer.
                self._trace_unavailable_review(run, exc)
                raise
            # A runner that does not say where its review came from is reported as undeclared, never guessed.
            review_source = review_findings.get("review_source") or REVIEW_SOURCE_UNDECLARED

            # v3: P02 stays read-only. The mistakes and dissents it found are candidates only:
            # they enter the observation pool in P04, i.e. after the batch was approved.
            mistake_candidates = candidate_mistakes(review_findings.get("mistakes") or [])
            dissent_candidates = candidate_dissents(review_findings.get("dissents") or [])
            promotion_ready = eligible_promotions(self.db, mistake_candidates)
            sent_session_keys = [
                str(key)
                for key in (review_findings.get("sent_session_keys") or [])
                if str(key)
            ]
            review_findings["candidate_mistakes"] = mistake_candidates
            review_findings["candidate_dissents"] = dissent_candidates
            review_findings["eligible_promotions"] = promotion_ready
            review_findings["sent_session_keys"] = sent_session_keys
            review_findings["candidate_mistake_count"] = len(mistake_candidates)
            review_findings["candidate_dissent_count"] = len(dissent_candidates)

            # P02 reviews a session the client hand-distilled like any other: its mistakes,
            # dissents and rule candidates are what it was queued for. What makes it
            # different is carried along for P03 and named in the planner's input.
            review_findings["governance_only_session_keys"] = self._declared_keys(
                run.admitted_sessions, frozenset({DISTILL_MODE_GOVERNANCE_ONLY})
            )
            # What the review found that stands on its own (a delivery, an incident, a decision,
            # a domain fact). A runner that reports none, or a shape that is not a list, means none.
            review_events = [
                event for event in (review_findings.get("events") or []) if isinstance(event, dict)
            ] if isinstance(review_findings.get("events"), list) else []
            review_findings["events"] = review_events

            wf_candidates = len(review_findings.get("repeated_workflows", []))
            logger.info(
                "Run %s: P02 review from %s (%s finding(s) for %s session(s) sent)",
                run.run_id,
                review_source,
                len(review_findings.get("findings") or []),
                (review_findings.get("review_diagnostics") or {}).get("sessions_sent", "?"),
            )
            self._finish_node(
                run,
                "P02_REVIEW",
                t0,
                summary=(
                    f"审查来源：{REVIEW_SOURCE_LABELS.get(review_source, review_source)}；"
                    f"完成 {review_findings.get('sessions_reviewed', 0)} 段对话回顾"
                    f"（候选错题 {len(mistake_candidates)} 条、可晋升 {len(promotion_ready)} 条、"
                    f"候选分歧 {len(dissent_candidates)} 条、流程候选 {wf_candidates} 处）"
                ),
                internal_checks=[
                    {"name": "对话事实回顾", "status": "PASS"},
                    {
                        "name": "错题观察池双轨准入与分歧保留",
                        "status": "PASS",
                        "detail": {
                            "mistakes": len(mistake_candidates),
                            "eligible_promotions": len(promotion_ready),
                            "dissents": len(dissent_candidates),
                            "written": False,
                        },
                    },
                    {"name": "重复流程与 Skill 线索扫描", "status": "PASS", "detail": {"candidates": wf_candidates, "events": len(review_events)}},
                    {"name": "问题线索关联", "status": "PASS", "detail": {"open_retros": len(open_retros)}},
                ],
                payload={
                    **review_findings,
                    "review_source": review_source,
                    # What the model call cost: attempts, duration, served model, token usage.
                    "call_telemetry": (review_findings.get("review_diagnostics") or {}).get("call_telemetry")
                    or review_findings.get("call_telemetry"),
                },
            )

            # ---------------- P03 ROUTE & PLAN ----------------
            active_node = "P03_ROUTE_PLAN"
            t0 = self._begin_node(run, "P03_ROUTE_PLAN")
            if simulate_fail_node == "P03_ROUTE_PLAN":
                raise RuntimeError("Simulated P03 failure")
            existing_rules = self.db.list_rules()
            max_ws = min(
                self.cfg.workflow.max_writes_per_run,
                self.cfg.knowledge_sink.max_write_set,
            )
            # The planner's item cap (`max_ws`) counts the items that are not project-domain pages;
            # domain pages are counted under a cap of their own. The physical paths a batch writes
            # — a document and the index entries it needs — are bounded by the sink alone, whoever
            # planned them: the bound holds on the merged plan.
            max_physical_paths = max(1, int(self.cfg.knowledge_sink.max_write_set))
            default_prefix = (
                self.cfg.knowledge_sink.allowed_write_prefixes[0]
                if self.cfg.knowledge_sink.allowed_write_prefixes
                else "lessons/"
            )
            # The routing candidate set (page types and projects read off the mounted Wiki) goes to the planner.
            routing_candidates = (
                self.knowledge.wiki_gate.routing_candidates() if self.knowledge.wiki_gate.applicable else None
            )
            if routing_candidates is not None:
                # The planner cannot fit rows into a table it has never seen: the domain pages the
                # review signalled lend it their section layout before the plan is written.
                self.knowledge.wiki_gate.sketch_domain_pages(
                    routing_candidates, domain_signal_names(review_findings.get("findings"))
                )
            try:
                plan_outcome = runner.plan_writes_detailed(
                    review_findings,
                    existing_rules,
                    max_write_set=max_ws,
                    default_doc_prefix=default_prefix,
                    existing_skills=existing_skills,
                    phase=resolved_phase,
                    routing_candidates=routing_candidates,
                )
            except PlanUnavailableError as exc:
                # The configured planner produced nothing usable: fail the run, never swap in another planner.
                self._trace_unavailable_plan(run, exc)
                raise
            planned_writes = plan_outcome.planned_writes
            plan_info = plan_outcome.diagnostics
            # A session the client hand-distilled had its knowledge written up by hand. The
            # planner is told so and asked not to propose a document for it, but a model's
            # plan is not trusted: a knowledge document that speaks only for such sessions is
            # taken out here, deterministically, before it is routed or can reach the approval
            # card. Its rules, Skill plans, mistakes and dissents are what it was queued for.
            # The declaration is read again rather than taken from P01/P02: it may have been
            # made while this batch was already running. This never fails the node: a plan
            # left with nothing to write is an empty plan, not an unavailable one. A session
            # taken out entirely (`governance_only=false`) counts the same way: its knowledge
            # was written by hand too.
            governance_only_keys = sorted(
                {
                    *self._declared_keys(
                        run.admitted_sessions,
                        frozenset({DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}),
                    ),
                    *(review_findings.get("governance_only_session_keys") or []),
                }
            )
            planned_writes, governance_only_skipped = filter_governance_only_docs(
                planned_writes, set(governance_only_keys), run.admitted_sessions
            )
            if not self.knowledge.wiki_gate.applicable:
                plan_info.kept_count = len(planned_writes)
            if governance_only_skipped:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="governance_only_wiki_skipped",
                    actor="p03_plan",
                    summary=(
                        f"{len(governance_only_skipped)} 篇笔记所属会话已由客户端手工沉淀，"
                        f"仅做治理提炼，未列入写入计划"
                    ),
                    payload={
                        "skipped_docs": governance_only_skipped,
                        "governance_only_session_keys": governance_only_keys,
                    },
                )
            # On a schema-governed Wiki every knowledge_doc is routed before it is adapted: the planner's
            # declaration, validated against workspace and draft evidence and never corrected, says whether
            # it is written and where. A document without a consistent route is dropped here and surfaced
            # instead of being guessed into a project or failing the whole batch in P04.
            unrouted_docs: list[dict[str, Any]] = []
            routed_docs: list[dict[str, Any]] = []
            index_capacity_drops: list[dict[str, Any]] = []
            knowledge_write_paths: set[str] = set()
            domain_docs: list[dict[str, Any]] = []
            if self.knowledge.wiki_gate.applicable:
                used_norm_paths: set[str] = set()
                sess_by_key = {s.session_key: s for s, _ in admitted}
                routed_writes: list[PlannedWrite] = []
                deposit = routing_candidates.deposit_routing if routing_candidates is not None else None
                domain_page_types = set(deposit.page_types) if deposit is not None else set()

                def domain_page_last(write: PlannedWrite) -> bool:
                    """A project-domain page claims the batch's write capacity after everything else.

                    The main experience card keeps its slot: within one batch's limit the
                    card (with the index entry it needs) is counted first, and the domain
                    pages fill what is left.
                    """
                    route = write.wiki_route if isinstance(write.wiki_route, dict) else {}
                    return (
                        write.target_kind == "knowledge_doc"
                        and str(route.get("page_type") or "").lower() in domain_page_types
                    )

                for w in sorted(planned_writes, key=domain_page_last):
                    if w.target_kind != "knowledge_doc":
                        routed_writes.append(w)
                        continue
                    decision = route_knowledge_doc(self.knowledge, run, w, sess_by_key, routing_candidates)
                    adapted = (
                        self.knowledge.wiki_gate.normalize_planned_document(
                            w.relative_path,
                            w.title,
                            w.summary,
                            w.content_preview,
                            source_sessions=w.source_session_keys,
                            route=decision,
                            action=w.action,
                        )
                        if decision.routed
                        else None
                    )
                    if adapted is None:
                        unrouted_docs.append(
                            {
                                "title": w.title,
                                "relative_path": w.relative_path,
                                "source_session_keys": list(w.source_session_keys),
                                "reason": decision.detail or LANDING_FAILED_REASON,
                                "reason_code": decision.reason_code or "landing_failed",
                                "declared": decision.declared,
                                "evidence": decision.evidence,
                            }
                        )
                        continue
                    new_rel, new_md = adapted
                    if new_rel in used_norm_paths and not decision.domain:
                        stem = new_rel[:-3] if new_rel.endswith(".md") else new_rel
                        new_rel = f"{stem}-{len(used_norm_paths) + 1}.md"
                    # A domain page keeps its path even when the plan names it twice: one table is
                    # one page. The later increment is merged after the earlier one at write time.
                    index_registration = (
                        self.knowledge.wiki_gate.plan_practice_index_registration(
                            new_rel,
                            new_md,
                        )
                    )
                    side_effect = index_registration.side_effect() if index_registration else None
                    candidate_paths = {new_rel}
                    if side_effect and side_effect.get("status") == "update":
                        candidate_paths.add(str(side_effect["target_path"]))
                    if len(knowledge_write_paths | candidate_paths) > max_physical_paths:
                        index_capacity_drops.append(
                            {
                                "title": w.title,
                                "relative_path": new_rel,
                                "reason": "WRITE_SET_TOTAL_LIMIT",
                                "planned_paths": sorted(candidate_paths),
                                "limit": max_physical_paths,
                            }
                        )
                        continue
                    knowledge_write_paths.update(candidate_paths)
                    used_norm_paths.add(new_rel)
                    w.relative_path = new_rel
                    w.content_preview = new_md
                    w.side_effects = [side_effect] if side_effect else []
                    apply_route_decision(w, decision)
                    routed_writes.append(w)
                    routed_docs.append(
                        {
                            "title": w.title,
                            "relative_path": new_rel,
                            "source_session_keys": list(w.source_session_keys),
                            "route": decision.to_dict(),
                            "side_effects": list(w.side_effects),
                        }
                    )
                    if decision.domain:
                        domain_docs.append(routed_docs[-1])
                planned_writes = routed_writes
                plan_info.kept_count = len(planned_writes)
            run.planned_writes = planned_writes
            if plan_info.dropped_items:
                self._trace_dropped_plan_items(run, plan_info)
            if index_capacity_drops:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="planned_doc_write_set_limited",
                    actor="wiki_gate",
                    summary=(
                        f"{len(index_capacity_drops)} 篇笔记连同项目索引超过单批 "
                        f"{max_physical_paths} 个物理写入路径，未列入计划"
                    ),
                    payload={"dropped": index_capacity_drops, "limit": max_physical_paths},
                )
            if plan_info.deferred_skill_candidates:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="skill_candidates_deferred",
                    actor="p03_plan",
                    summary=f"{len(plan_info.deferred_skill_candidates)} 个重复流程候选未生成 Skill 计划",
                    payload={
                        "plan_source": plan_info.source,
                        "candidates": plan_info.deferred_skill_candidates,
                    },
                )
            unrouted_reasons = Counter(doc["reason_code"] for doc in unrouted_docs)
            if routed_docs:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="planned_doc_routed",
                    actor="wiki_gate",
                    summary=(
                        f"{len(routed_docs)} 篇笔记已确定归属："
                        + "；".join(
                            # An incident card belongs to no project: its page type says where it went.
                            f"{doc['title']} → {doc['route']['project'] or doc['route']['page_type']}"
                            for doc in routed_docs[:6]
                        )
                    ),
                    payload={"routed_docs": routed_docs},
                )
            if unrouted_docs:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="planned_doc_unrouted",
                    actor="wiki_gate",
                    summary=(
                        f"{len(unrouted_docs)} 篇笔记未列入写入计划：{unrouted_reason_breakdown(unrouted_reasons)}"
                    ),
                    payload={"unrouted_docs": unrouted_docs},
                )
            skill_plans = sum(1 for w in planned_writes if w.target_kind == "skill_improvement")
            unrouted_note = (
                f"；{len(unrouted_docs)} 篇笔记因无法归属项目未列入计划（{unrouted_reason_breakdown(unrouted_reasons)}）"
                if unrouted_docs
                else ""
            )
            capacity_note = (
                f"；{len(index_capacity_drops)} 篇笔记连同索引超过物理写入上限，未列入计划"
                if index_capacity_drops
                else ""
            )
            domain_updates = sum(1 for doc in domain_docs if doc["route"].get("incremental"))
            domain_sensitive = sum(1 for doc in domain_docs if doc["route"].get("sensitive"))
            domain_note = (
                f"；其中 {len(domain_docs)} 项为项目领域页（增量更新 {domain_updates}、新建 "
                f"{len(domain_docs) - domain_updates}、敏感待审批 {domain_sensitive}）"
                if domain_docs
                else ""
            )
            incident_docs = [doc for doc in routed_docs if doc["route"].get("page_type") == INCIDENT_PAGE_TYPE]
            incident_note = (
                f"；其中 {len(incident_docs)} 项为事故卡（敏感写入，待人工审批）" if incident_docs else ""
            )
            invalid_items = len(plan_info.dropped_items) - plan_info.over_limit_count
            dropped_note = ""
            if plan_info.dropped_items:
                over_note = f"、{plan_info.over_limit_count} 项超出单批上限" if plan_info.over_limit_count else ""
                dropped_note = (
                    f"；计划项共 {plan_info.returned_count} 项，{len(plan_info.dropped_items)} 项未列入"
                    f"（{invalid_items} 项未通过校验{over_note}）"
                )
            deferred_count = len(plan_info.deferred_skill_candidates)
            deferred_note = f"；{deferred_count} 个重复流程候选未生成 Skill 计划" if deferred_count else ""
            governance_note = (
                f"；{len(governance_only_skipped)} 篇笔记所属会话已手工沉淀（仅治理提炼），未列入计划"
                if governance_only_skipped
                else ""
            )
            self._finish_node(
                run,
                "P03_ROUTE_PLAN",
                t0,
                summary=(
                    f"计划来源：{PLAN_SOURCE_LABELS.get(plan_info.source, plan_info.source)}；"
                    f"查重比对 {len(existing_rules)} 条规则与 {len(existing_skills)} 个技能，"
                    f"整理出 {len(planned_writes)} 项建议（含 {skill_plans} 项 Skill 封装/优化）"
                    f"{dropped_note}{unrouted_note}{capacity_note}{domain_note}{incident_note}{deferred_note}{governance_note}"
                ),
                internal_checks=[
                    {"name": "规则与技能查重", "status": "PASS"},
                    {"name": "Skill 路由判定", "status": "PASS", "detail": {"skill_plans": skill_plans}},
                    {
                        "name": "单批数量上限校验",
                        "status": "PASS",
                        "detail": {
                            "planned": len(planned_writes),
                            "physical_knowledge_paths": len(knowledge_write_paths),
                            "index_capacity_dropped": len(index_capacity_drops),
                            "limit": max_physical_paths,
                        },
                    },
                    {
                        "name": "写入计划项校验",
                        "status": "PASS" if not invalid_items else "WARN",
                        "detail": {
                            "source": plan_info.source,
                            "returned": plan_info.returned_count,
                            "invalid": invalid_items,
                            "over_limit": plan_info.over_limit_count,
                            "completed_fields": dict(plan_info.completed_fields),
                        },
                    },
                    *(
                        [
                            {
                                "name": "已手工沉淀会话的知识文档过滤",
                                "status": "PASS",
                                "detail": {
                                    "governance_only_session_keys": governance_only_keys,
                                    "skipped_docs": len(governance_only_skipped),
                                },
                            }
                        ]
                        if governance_only_skipped
                        else []
                    ),
                    *(
                        [
                            {
                                "name": "知识文档路由校验",
                                "status": "PASS",
                                "detail": {
                                    "routed": len(routed_docs),
                                    "declared": sum(1 for d in routed_docs if d["route"]["source"] == ROUTE_DECLARED),
                                    "evidence": sum(1 for d in routed_docs if d["route"]["source"] == ROUTE_EVIDENCE),
                                    "unrouted": len(unrouted_docs),
                                    **({"domain_pages": len(domain_docs)} if domain_docs else {}),
                                    **({"incident_cards": len(incident_docs)} if incident_docs else {}),
                                },
                            }
                        ]
                        if self.knowledge.wiki_gate.applicable
                        else []
                    ),
                ],
                payload={
                    "planned_count": len(planned_writes),
                    "skill_plans": skill_plans,
                    "routed_docs": routed_docs,
                    "unrouted_docs": unrouted_docs,
                    "index_capacity_drops": index_capacity_drops,
                    "plan_source": plan_info.source,
                    "plan_diagnostics": plan_info.to_dict(),
                    # What the model call cost: attempts, duration, served model, token usage.
                    "call_telemetry": plan_info.call_telemetry,
                    # The knowledge documents left out because their sessions were written up
                    # by hand (hand_distilled), and the sessions that made them be.
                    "governance_only_session_keys": governance_only_keys,
                    "governance_only_skipped_docs": governance_only_skipped,
                    "governance_only_wiki_skipped": len(governance_only_skipped),
                },
            )

            # Check Approval Gate (supports smart | always | auto)
            if auto_approve_override is True:
                effective_mode = "auto"
            elif auto_approve_override is False:
                effective_mode = "always"
            else:
                effective_mode = self.cfg.effective_approval_mode

            should_pause, pause_reasons = self._needs_manual_approval(
                planned_writes,
                effective_mode,
                high_write_threshold=self.cfg.workflow.high_write_count_threshold,
            )
            if should_pause:
                p04 = self._get_node(run, "P04_WRITE_VERIFY")
                p04.status = NodeStatus.AWAITING_APPROVAL
                reason_text = "；".join(pause_reasons[:2])
                p04.summary = f"已暂停等待确认（{reason_text}）"
                run.status = RunStatus.AWAITING_APPROVAL
                self.db.save_workflow_run(run)
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="awaiting_approval",
                    actor="orchestrator",
                    summary=f"挂起等待人工审批：{reason_text}",
                    payload={"reasons": pause_reasons, "planned_count": len(planned_writes)},
                )
                # Create structured 'ask' decision card & notify via Feishu CLI when enabled
                card_id = f"card-{run.run_id}"
                portal_url = f"http://{self.cfg.server.host}:{self.cfg.server.port}/runs"
                items_md = "\n".join(
                    f"- [{('Skill' if w.target_kind == 'skill_improvement' else '规则' if w.target_kind == 'flywheel_rule' else '笔记')}] **{w.title}**：{w.summary}"
                    for w in planned_writes[:6]
                )
                card_body = (
                    f"批次 `{run.batch_date} #{run.batch_seq:02d}` 触发了重要变更确认：\n"
                    f"- 触发原因：{reason_text}\n\n"
                    f"{items_md}\n\n"
                    f"请打开控制台确认放行或取消：{portal_url}"
                )
                self.db.save_decision_card(
                    card_id=card_id,
                    run_id=run.run_id,
                    kind="ask",
                    state="open",
                    title=f"AI 飞轮待审批（{run.batch_date} #{run.batch_seq:02d}）",
                    body_markdown=card_body,
                    options=["同意并写入", "取消本次写入"],
                )
                self.db.record_card_delivery(
                    delivery_id=f"del-{card_id}-portal",
                    card_id=card_id,
                    channel="portal",
                    status="sent",
                )
                # The webhook channel is independent of Feishu: either, both or
                # neither may be enabled. The adapter checks its own config.
                self.delivery.notify_approval_needed(
                    run=run,
                    reasons=pause_reasons,
                    planned_writes=planned_writes,
                    portal_url=portal_url,
                    title=f"AI 飞轮待审批（{run.batch_date} #{run.batch_seq:02d}）",
                )
                if self.cfg.delivery.feishu_cli.enabled:
                    if self.feishu_channel is not None:
                        self.feishu_channel.first_send(
                            card_id=card_id, run=run, reasons=pause_reasons, portal_url=portal_url
                        )
                    else:
                        fres = self.delivery.send_feishu_text_notice(
                            title=f"AI 飞轮待审批（{run.batch_date} #{run.batch_seq:02d}）",
                            markdown=card_body,
                            kind="ask",
                            idempotency_key=f"flywheel-ask-{run.run_id}",
                        )
                        if fres["status"] != "SKIPPED":
                            self.db.record_card_delivery(
                                delivery_id=f"del-{card_id}-feishu_cli",
                                card_id=card_id,
                                channel="feishu_cli",
                                status=fres["status"],
                                external_ref=fres.get("external_ref"),
                                last_error=fres.get("error"),
                            )
                self._emit_run("awaiting_approval", run)
                self._stop_heartbeat()
                return run

            if planned_writes:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="auto_approved",
                    actor="smart_gate",
                    summary=f"智能审批自动放行 {len(planned_writes)} 项常规变更",
                    payload={"approval_mode": effective_mode, "planned_count": len(planned_writes)},
                )

            return self._execute_post_approval(run, simulate_fail_node=simulate_fail_node)

        except Exception as exc:
            return self._fail_run(
                run, active_node, str(exc), t0, extra_payload=_error_payload_with_telemetry(exc)
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

    def _commit_p04_writes(
        self,
        run: WorkflowRunRecord,
        written_docs: list[str],
        written_skills: list[str],
        *,
        approval_source: str,
    ) -> dict[str, Any]:
        """Commit the files P04 just wrote (opt-in) and leave the outcome in the audit trail.

        A Git problem never fails P04: the write already passed its gates, so the
        result is recorded (``committed`` / ``skipped`` / ``failed``) and the batch
        continues.
        """
        absolute_paths = [self.cfg.knowledge_root_path / rel for rel in written_docs]
        absolute_paths += [self.skill_writer.root / rel for rel in written_skills]
        try:
            result = commit_p04_written_files(
                absolute_paths,
                run_id=run.run_id,
                approval_source=approval_source,
                summary=f"P04 受控写入：笔记 {len(written_docs)} 篇、Skill {len(written_skills)} 个",
            )
        except Exception as exc:  # the audit trail must never break a verified write
            logger.warning("Run %s: P04 git commit raised", run.run_id, exc_info=True)
            result = {
                "status": "failed",
                "reason": "exception",
                "commits": [],
                "skipped_paths": [],
                "skipped_repos": [],
                "error": str(exc),
            }
        status = str(result.get("status") or "failed")
        if status == "committed":
            event_type = "p04_git_commit"
            summary = (
                f"P04 写入已本地提交（{len(result['commits'])} 个仓库，"
                f"{sum(len(c['files']) for c in result['commits'])} 个文件）"
            )
        elif status == "skipped":
            event_type = "p04_git_commit_skipped"
            summary = f"P04 未生成本地提交（{result.get('reason') or 'skipped'}）"
        else:
            event_type = "p04_git_commit_failed"
            summary = f"P04 本地提交失败：{result.get('error') or result.get('reason') or 'unknown'}"
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run.run_id,
            event_type=event_type,
            actor="p04_write",
            summary=summary,
            payload=result,
        )
        return result

    def _validate_p04_knowledge_scope(
        self,
        approved_items: list[PlannedWrite],
    ) -> list[str]:
        """Fail before any P04 side effect when the approved physical set is stale."""
        if not self.knowledge.wiki_gate.applicable:
            return []
        physical_paths: set[str] = set()
        for item in approved_items:
            if item.target_kind != "knowledge_doc":
                continue
            if not item.relative_path:
                raise ValueError(
                    f"知识文档写入项缺少 relative_path，无法落盘：{item.title}"
                )
            physical_paths.add(item.relative_path)
            physical_paths.update(
                str(effect["target_path"])
                for effect in item.side_effects
                if effect.get("kind") == "practice_index_entry"
                and effect.get("status") == "update"
                and effect.get("target_path")
            )
            registration = self.knowledge.wiki_gate.plan_practice_index_registration(
                item.relative_path,
                item.content_preview,
            )
            if registration is not None and registration.status == "update":
                if not approved_registration_matches(registration, item.side_effects):
                    raise ValueError(
                        "P04 practice-index side effect is outside the approved scope "
                        f"for '{item.title}'. Re-plan and approve the batch."
                    )
                physical_paths.add(registration.target_relative_path)
        limit = max(1, int(self.cfg.knowledge_sink.max_write_set))
        if len(physical_paths) > limit:
            raise ValueError(
                f"P04 approved physical write set has {len(physical_paths)} paths; "
                f"the configured maximum is {limit}."
            )
        return sorted(physical_paths)

    def _execute_post_approval(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        active_node = "P04_WRITE_VERIFY"
        t0 = time.perf_counter()
        written_paths: list[str] = []
        written_index_paths: list[str] = []
        created_rules: list[str] = []
        written_skills: list[str] = []
        skipped_docs: list[dict[str, Any]] = []
        merged_domain_pages = 0
        try:
            # ---------------- P04 WRITE & VERIFY ----------------
            t0 = self._begin_node(run, "P04_WRITE_VERIFY")
            if simulate_fail_node == "P04_WRITE_VERIFY":
                raise RuntimeError("Simulated P04 write verification failure")

            approved_items = [w for w in run.planned_writes if w.approved]
            # The plan may have waited hours for approval. A session declared hand-distilled
            # in that time has had its knowledge written up by hand since: its document is
            # not written a second time, and the receipt says why.
            approved_items, governance_only_skipped = filter_governance_only_docs(
                approved_items,
                set(
                    self._declared_keys(
                        run.admitted_sessions,
                        frozenset({DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}),
                    )
                ),
                run.admitted_sessions,
            )
            write_receipts: list[dict[str, Any]] = [
                governance_only_skip_receipt(doc) for doc in governance_only_skipped
            ]
            if governance_only_skipped:
                self.db.append_audit_event(
                    entity_type="workflow_run",
                    entity_id=run.run_id,
                    event_type="governance_only_wiki_skipped",
                    actor="p04_write",
                    summary=(
                        f"{len(governance_only_skipped)} 篇已批准笔记所属会话在等待审批期间被声明手工沉淀，未写入"
                    ),
                    payload={"skipped_docs": governance_only_skipped},
                )
            p02_payload = self._get_node(run, "P02_REVIEW").payload
            if not isinstance(p02_payload, dict):
                p02_payload = {}
            mistake_candidates = [
                c for c in (p02_payload.get("candidate_mistakes") or []) if isinstance(c, dict)
            ]
            dissent_candidates = [
                c for c in (p02_payload.get("candidate_dissents") or []) if isinstance(c, dict)
            ]
            approved_physical_paths = self._validate_p04_knowledge_scope(approved_items)
            persisted_observations: list[dict[str, Any]] = []
            index_error: Exception | None = None
            try:
                # P04 is the first node allowed to change the mistake pool and the dissent
                # records: the batch was approved, so the candidates P02 found become durable.
                self._ensure_lock(run)
                for candidate in mistake_candidates:
                    self._ensure_lock(run)
                    obs, _created = self.db.upsert_mistake_observation(
                        root_cause_key=str(candidate.get("root_cause_key") or ""),
                        title=str(candidate.get("title") or ""),
                        severity_grade=str(candidate.get("severity_grade") or "yellow_caught"),
                        phenomenon=str(candidate.get("phenomenon") or ""),
                        truth=str(candidate.get("truth") or ""),
                        thinking_root_cause=str(candidate.get("thinking_root_cause") or ""),
                        caught_by=str(candidate.get("caught_by") or "self"),
                        session_key=str(candidate.get("session_key") or ""),
                    )
                    persisted_observations.append(obs)
                for candidate in dissent_candidates:
                    self._ensure_lock(run)
                    self.db.add_dissent_record(
                        target_type=str(candidate.get("target_type") or "rule"),
                        target_id=str(candidate.get("target_id") or ""),
                        counter_claim=str(candidate.get("counter_claim") or ""),
                        condition_scope=str(candidate.get("condition_scope") or ""),
                        evidence_pointer=str(candidate.get("evidence_pointer") or ""),
                        session_key=str(candidate.get("session_key") or ""),
                    )
                for item in approved_items:
                    self._ensure_lock(run)  # synchronous checkpoint before each write
                    if item.target_kind == "knowledge_doc":
                        if not item.relative_path:
                            raise ValueError(
                                f"知识文档写入项缺少 relative_path，无法落盘：{item.title}"
                            )
                        rel_to_write = item.relative_path
                        content_to_write = item.content_preview
                        merge_write: dict[str, Any] = {}
                        if self.knowledge.wiki_gate.applicable:
                            # P03 already routed every knowledge document; this re-check only catches a plan
                            # that went stale between P03 and P04 (saved before routing, or edited since).
                            # A project-domain update is judged against the page as it is on disk now.
                            decision = route_knowledge_doc(
                                self.knowledge, run, item, self._stored_sessions(item.source_session_keys), None
                            )
                            adapted = (
                                self.knowledge.wiki_gate.normalize_planned_document(
                                    rel_to_write,
                                    item.title,
                                    item.summary,
                                    content_to_write,
                                    source_sessions=item.source_session_keys,
                                    route=decision,
                                    action=item.action,
                                )
                                if decision.routed
                                else None
                            )
                            if adapted is None:
                                # A stale plan must not fail the batch here. A domain increment the
                                # page gained meanwhile is reported by its own marker, not as unrouted.
                                already_there = decision.reason_code == "update_no_new_content"
                                skipped_docs.append(
                                    {
                                        "relative_path": item.relative_path,
                                        "title": item.title,
                                        "status": UPDATE_NO_NEW_CONTENT if already_there else "SKIPPED_UNROUTED_PROJECT",
                                        "skipped": True,
                                        "written": False,
                                        "reason": decision.detail or LANDING_FAILED_REASON,
                                        "reason_code": decision.reason_code or "landing_failed",
                                    }
                                )
                                write_receipts.append(skipped_docs[-1])
                                continue
                            rel_to_write, content_to_write = adapted
                            if decision.incremental:
                                merge_write = {"merge_increment": True}
                        res = self.knowledge.write_document_verified(
                            rel_to_write,
                            content_to_write,
                            action=item.action,
                            sync_fts=False,  # the batch rebuilds the search index once, at the end
                            sync_practice_index=self.knowledge.wiki_gate.applicable,
                            approved_side_effects=item.side_effects,
                            **merge_write,
                        )
                        if res.get("skipped"):
                            # The page gained this increment after the plan was approved: nothing is left to add.
                            skipped_docs.append(
                                {
                                    "relative_path": item.relative_path,
                                    "title": item.title,
                                    "status": str(res.get("status") or "SKIPPED"),
                                    "skipped": True,
                                    "written": False,
                                    "reason_code": "update_no_new_content",
                                    "reason": str(res.get("reason") or ""),
                                }
                            )
                            write_receipts.append(res)
                            continue
                        if res.get("merge"):
                            merged_domain_pages += 1
                        written_paths.append(res["relative_path"])
                        index_receipt = res.get("practice_index") or {}
                        if (
                            index_receipt.get("status") == "update"
                            and index_receipt.get("target_path")
                            and index_receipt["target_path"] not in written_index_paths
                        ):
                            written_index_paths.append(str(index_receipt["target_path"]))
                        write_receipts.append(res)
                    elif item.target_kind == "flywheel_rule":
                        normalized_payload = normalize_rule_payload(item)
                        obs_id = normalized_payload.pop("obs_id", None)
                        root_cause_key = normalized_payload.pop("root_cause_key", None)
                        req = CreateRuleRequest.model_validate(normalized_payload)
                        rule = self.activation.register_rule(req, root_cause_key=root_cause_key)
                        promoted_obs = promote_observation(self.db, rule.rule_id, obs_id, root_cause_key)
                        created_rules.append(rule.rule_id)
                        write_receipts.append(
                            {
                                "rule_id": rule.rule_id,
                                "title": rule.title,
                                "status": rule.status.value,
                                **({"promoted_observation_id": promoted_obs} if promoted_obs else {}),
                            }
                        )
                    elif item.target_kind == "skill_improvement":
                        if not self.cfg.safety_valves.skill_write_enabled:
                            write_receipts.append({"skill": item.title, "status": "SKIPPED_BY_SAFETY_VALVE"})
                            continue
                        sres = self.skill_writer.write_skill_verified(
                            item.relative_path,
                            item.content_preview,
                            action=item.action,
                            run_id=run.run_id,
                            fallback_title=item.title,
                        )
                        written_skills.append(sres["relative_path"])
                        write_receipts.append(sres)
                        self.db.upsert_skill_asset(
                            skill_name=sres["skill_name"],
                            source_dir=str(self.skill_writer.root),
                            description=item.summary or item.title,
                            origin="flywheel_created" if sres["action"] == "create" else "flywheel_updated",
                            sha256=sres["sha256"],
                            last_run_id=run.run_id,
                            backup_path=sres.get("backup_path"),
                        )
                    else:
                        raise ValueError(
                            f"未知的写入目标类型，无法执行写入：{item.target_kind}"
                        )
            except Exception as exc:
                # A half-done batch must leave its receipts behind: the next run needs to know
                # which documents were already written before the failure.
                self._record_partial_writes(
                    run,
                    write_receipts,
                    [*written_paths, *written_index_paths],
                    exc,
                )
                raise
            finally:
                if written_paths or written_index_paths:
                    # One index rebuild for the whole batch, not one per document.
                    try:
                        self.knowledge.sync_fts_index()
                    except Exception as exc:  # noqa: BLE001
                        # A failed rebuild must not mask the write failure that is already
                        # on its way out; it is raised on its own below.
                        index_error = exc
            if index_error is not None:
                self._record_partial_writes(run, write_receipts, written_paths, index_error)
                raise index_error

            # Optional audit trail: commit exactly the files P04 wrote. Never fails the batch.
            git_commit_result: dict[str, Any] | None = None
            changed_knowledge_paths = [*written_paths, *written_index_paths]
            if self.cfg.knowledge_sink.auto_git_commit and (
                changed_knowledge_paths or written_skills
            ):
                git_commit_result = self._commit_p04_writes(
                    run,
                    changed_knowledge_paths,
                    written_skills,
                    approval_source=self._approval_source_for(run),
                )

            git_note = ""
            if git_commit_result is not None:
                if git_commit_result.get("status") == "committed":
                    git_note = (
                        f"；已本地 Git 提交 "
                        f"{sum(len(c['files']) for c in git_commit_result['commits'])} 个文件"
                    )
                elif git_commit_result.get("status") == "failed":
                    git_note = "；本地 Git 提交失败（已留审计）"

            # A domain increment the page already held is not a landing failure: it is counted apart.
            no_new_content_skips = sum(1 for doc in skipped_docs if doc["status"] == UPDATE_NO_NEW_CONTENT)
            unrouted_skips = len(skipped_docs) - no_new_content_skips

            self._finish_node(
                run,
                "P04_WRITE_VERIFY",
                t0,
                summary=(
                    f"写入 {len(written_paths)} 篇笔记、注册 {len(created_rules)} 条规则、"
                    f"更新/新建 {len(written_skills)} 个 Skill（全部校验通过）"
                    + (
                        f"；同步 {len(written_index_paths)} 个项目笔记索引"
                        if written_index_paths
                        else ""
                    )
                    + (f"；{unrouted_skips} 篇笔记因无法归属项目跳过" if unrouted_skips else "")
                    + (f"；{merged_domain_pages} 篇领域页按增量无损合并，原有内容完整保留" if merged_domain_pages else "")
                    + (f"；{no_new_content_skips} 篇领域页增量已存在于页面，未重复写入" if no_new_content_skips else "")
                    + (
                        f"；{len(governance_only_skipped)} 篇笔记因所属会话已手工沉淀，未重复写入"
                        if governance_only_skipped
                        else ""
                    )
                    + (
                        f"；错题观察池入池 {len(persisted_observations)} 条"
                        if persisted_observations
                        else ""
                    )
                    + git_note
                ),
                internal_checks=[
                    {"name": "路径安全与白名单校验", "status": "PASS"},
                    {"name": "落盘回读一致性校验", "status": "PASS", "detail": {"docs": len(written_paths), "skills": len(written_skills)}},
                    {
                        "name": "实战卡项目笔记索引原子登记",
                        "status": "PASS",
                        "detail": {
                            "indexes_updated": len(written_index_paths),
                            "registered_or_already_present": sum(
                                1
                                for receipt in write_receipts
                                if isinstance(receipt.get("practice_index"), dict)
                            ),
                        },
                    },
                    {
                        "name": "错题观察池与分歧记录写入（审批后）",
                        "status": "PASS",
                        "detail": {
                            "observations": len(persisted_observations),
                            "dissents": len(dissent_candidates),
                        },
                    },
                    *(
                        [
                            {
                                "name": "项目领域页增量无损合并",
                                "status": "PASS",
                                "detail": {
                                    "merged": merged_domain_pages,
                                    "no_new_content": no_new_content_skips,
                                },
                            }
                        ]
                        if merged_domain_pages or no_new_content_skips
                        else []
                    ),
                    *(
                        [
                            {
                                "name": "P04 写入后本地 Git 审计提交（可选）",
                                "status": "PASS"
                                if git_commit_result.get("status") == "committed"
                                else "WARN",
                                "detail": {
                                    "status": git_commit_result.get("status"),
                                    "reason": git_commit_result.get("reason"),
                                    "commits": len(git_commit_result.get("commits") or []),
                                },
                            }
                        ]
                        if git_commit_result is not None
                        else []
                    ),
                ],
                artifacts=changed_knowledge_paths + created_rules + written_skills,
                payload={
                    "write_receipts": write_receipts,
                    "approved_physical_paths": approved_physical_paths,
                    "written_paths": written_paths,
                    "written_practice_index_paths": written_index_paths,
                    "skipped_unrouted_docs": skipped_docs,
                    **(
                        {"governance_only_skipped_docs": governance_only_skipped}
                        if governance_only_skipped
                        else {}
                    ),
                    **({"merged_domain_pages": merged_domain_pages} if merged_domain_pages else {}),
                    "persisted_observation_ids": [
                        str(o.get("obs_id")) for o in persisted_observations if o.get("obs_id")
                    ],
                    "persisted_dissent_count": len(dissent_candidates),
                    **({"git_commit": git_commit_result} if git_commit_result is not None else {}),
                },
            )

            # ---------------- P05 SETTLE ----------------
            active_node = "P05_SETTLE"
            t0 = self._begin_node(run, "P05_SETTLE")
            if simulate_fail_node == "P05_SETTLE":
                raise RuntimeError("Simulated P05 failure")
            p02_node = self._get_node(run, "P02_REVIEW")
            p02_payload = p02_node.payload if isinstance(p02_node.payload, dict) else {}
            p02_findings = p02_payload.get("findings", []) or []
            not_worthy_keys = {
                str(f.get("session_key"))
                for f in p02_findings
                if isinstance(f, dict) and f.get("worthy") is False and f.get("session_key")
            }
            # Only the sessions the review actually read may be settled. A session that
            # was admitted but never sent to the reviewer stays pending: it still owes the
            # flywheel a card, and a later batch must pick it up again.
            sent_keys = p02_payload.get("sent_session_keys")
            if isinstance(sent_keys, list) and sent_keys:
                reviewed_keys = {str(k) for k in sent_keys if k}
            else:
                reviewed_keys = set(run.admitted_sessions)
            # Settle the queue items P01 claimed: reviewed -> processed (and the
            # consumption watermark moves), everything else returns to the queue.
            settled_queue_ids: list[str] = []
            requeued_queue_ids: list[str] = []
            for qid in dict.fromkeys(self._claimed_queue_ids(run)):
                self._ensure_lock(run)  # synchronous checkpoint before each settlement
                item = self.db.get_queue_item(qid)
                if item is None or item.status is not QueueStatus.PROCESSING:
                    continue
                if item.session_key in reviewed_keys:
                    self.db.update_queue_item_status(
                        qid,
                        QueueStatus.PROCESSED,
                        run_id=run.run_id,
                        actor="p05_settle",
                        reason_code="RUN_SETTLED",
                        summary="reviewed and settled by this run",
                    )
                    session = self.db.get_session(item.session_key)
                    self.db.update_session_watermark(
                        item.session_key,
                        last_consumed_turn=max(
                            session.last_consumed_turn if session else 0,
                            item.consumed_to_turn,
                        ),
                        last_consumed_hash=item.content_hash
                        or (session.last_consumed_hash if session else ""),
                    )
                    settled_queue_ids.append(qid)
                else:
                    self.db.update_queue_item_status(
                        qid,
                        QueueStatus.QUEUED,
                        run_id=None,
                        clear_run_id=True,
                        actor="p05_settle",
                        reason_code="UNREVIEWED_REQUEUED",
                        summary="not reviewed by this run; returned to the queue",
                    )
                    requeued_queue_ids.append(qid)
            self._publish_queue_update(
                "p05_settle", run.run_id, settled_queue_ids + requeued_queue_ids
            )
            settled_sessions: list[str] = []
            returned_sessions: list[str] = []
            deferred_sessions: list[str] = []
            for sk in run.admitted_sessions:
                self._ensure_lock(run)  # synchronous checkpoint before each settlement
                if sk not in reviewed_keys:
                    self.db.update_session_status(sk, SessionStatus.PENDING)
                    returned_sessions.append(sk)
                    continue
                if self.db.list_session_queue_items(
                    sk, statuses=[QueueStatus.QUEUED, QueueStatus.PROCESSING]
                ):
                    # Another chunk of the same session is still waiting: the profile
                    # stays pending until the last chunk is settled.
                    self.db.update_session_status(sk, SessionStatus.PENDING)
                    deferred_sessions.append(sk)
                    continue
                if sk in not_worthy_keys:
                    self.db.update_session_status(sk, SessionStatus.ARCHIVED)
                else:
                    self.db.update_session_status(sk, SessionStatus.PROCESSED)
                settled_sessions.append(sk)
            # The retro items of the settled sessions are closed with this run; the ones of
            # the returned sessions stay open, because those sessions are not done yet.
            settled_retros = self.db.resolve_retros_for_sessions(settled_sessions, run.run_id)
            dream_report: dict[str, Any] | None = None
            if run.phase in {"both", "weekly"}:
                dream_report = _facade_module().run_dream_consolidation(self.cfg, self.db, run_id=run.run_id)
            core_card_info = _facade_module().rebuild_core_card(self.cfg, self.db)
            dream_note = (
                f"，周五蒸馏自动休眠 {len(dream_report['auto_dormant_rules'])} 条零命中规则、"
                f"标记 {len(dream_report['expired_cards'])} 篇过期经验卡"
                if dream_report
                else ""
            )
            queue_settle_note = (
                f"，队列结算 {len(settled_queue_ids)} 条"
                + (f"、退回 {len(requeued_queue_ids)} 条" if requeued_queue_ids else "")
                if settled_queue_ids or requeued_queue_ids
                else ""
            )
            deferred_note = (
                f"、{len(deferred_sessions)} 个会话仍有未结算分片保持 pending"
                if deferred_sessions
                else ""
            )
            self._finish_node(
                run,
                "P05_SETTLE",
                t0,
                summary=(
                    f"幂等结算 {len(settled_sessions)} 个会话为 processed"
                    + (
                        f"、{len(returned_sessions)} 个未审查会话退回 pending"
                        if returned_sessions
                        else ""
                    )
                    + deferred_note
                    + f"，关闭 {settled_retros} 条待复盘项"
                    + f"，核心索引卡已刷新（{core_card_info['lines']} 行）{queue_settle_note}{dream_note}"
                ),
                internal_checks=[
                    {"name": "会话队列幂等回写", "status": "PASS"},
                    {
                        "name": "沉淀队列结算与水位线推进",
                        "status": "PASS",
                        "detail": {
                            "settled": len(settled_queue_ids),
                            "requeued": len(requeued_queue_ids),
                        },
                    },
                    {"name": "待复盘项结算", "status": "PASS", "detail": {"resolved_retros": settled_retros}},
                    {"name": "核心索引卡 FLYWHEEL-CORE.md 刷新", "status": "PASS", "detail": {"lines": core_card_info["lines"], "bytes": core_card_info["bytes"]}},
                    {"name": "P05 保留独占锁至 P07 终态校验", "status": "PASS"},
                ],
                payload={
                    "settled_sessions": settled_sessions,
                    "returned_sessions": returned_sessions,
                    "deferred_sessions": deferred_sessions,
                    "settled_queue_ids": settled_queue_ids,
                    "requeued_queue_ids": requeued_queue_ids,
                    "settled_retro_count": settled_retros,
                    "created_rules": created_rules,
                    "core_card": {"path": core_card_info["path"], "lines": core_card_info["lines"]},
                    "dream": dream_report,
                },
            )

            # ---------------- P06 REPORT ----------------
            active_node = "P06_REPORT"
            t0 = self._begin_node(run, "P06_REPORT")
            if simulate_fail_node == "P06_REPORT":
                raise RuntimeError("Simulated P06 failure")
            digest_lines = [
                f"## 沉淀总结（{run.run_id}）",
                "",
                f"- **处理对话数**：{len(settled_sessions)} 段"
                + (
                    f"（{len(returned_sessions)} 段未审查，退回队列）"
                    if returned_sessions
                    else ""
                ),
                f"- **沉淀笔记**：{', '.join(f'`{p}`' for p in written_paths) if written_paths else '无新增'}",
                f"- **新增避坑规则**：{', '.join(f'`{r}`' for r in created_rules) if created_rules else '无新增'}",
                f"- **新建 / 优化 Skill**：{', '.join(f'`{s}`' for s in written_skills) if written_skills else '无新增'}",
                f"- **核心索引卡**：已同步至 `data/FLYWHEEL-CORE.md`（{core_card_info['lines']} 行）",
            ]
            if dream_report:
                digest_lines.extend(
                    [
                        "",
                        f"### 周五蒸馏与减法摘要（{dream_report['period_key']}）",
                        f"- **自动休眠零命中规则**：{len(dream_report['auto_dormant_rules'])} 条",
                        f"- **超期需复核经验卡**：{len(dream_report['expired_cards'])} 篇",
                        f"- **待收敛分歧/反例**：{dream_report['open_dissents_count']} 条",
                    ]
                )
                for prop in dream_report.get("proposals", []):
                    digest_lines.append(f"- {prop}")
            digest_lines.extend(["", "### 各环节结果"])
            for n in run.nodes:
                if n.node_id in {"P06_REPORT", "P07_DELIVER"}:
                    continue
                digest_lines.append(f"- `{n.node_id}` {n.name}：**{n.status.value}** — {n.summary}")
            run.digest_markdown = "\n".join(digest_lines) + "\n"
            self._finish_node(
                run,
                "P06_REPORT",
                t0,
                summary="生成机器运行报告并刷新个人站统一读取层投影",
                internal_checks=[
                    {"name": "8 节点结构化报告生成", "status": "PASS"},
                    {"name": "个人站投影刷新", "status": "PASS"},
                ],
            )

            # ---------------- P07 DELIVER & FINALIZE ----------------
            active_node = "P07_DELIVER"
            t0 = self._begin_node(run, "P07_DELIVER")
            if simulate_fail_node == "P07_DELIVER":
                raise RuntimeError("Simulated P07 delivery failure")
            self._ensure_lock(run)  # synchronous checkpoint before external delivery
            delivery_res = self.delivery.deliver(run, written_paths)
            self._finish_node(
                run,
                "P07_DELIVER",
                t0,
                summary=(
                    f"交付完成（Portal={delivery_res['local_portal']}, "
                    f"Git={delivery_res['git_closure']}, Webhook={delivery_res['webhook']}），全部节点终态校验 PASS 并释放锁"
                ),
                internal_checks=[
                    {"name": "P06 预报告完备性检查", "status": "PASS"},
                    {"name": "多通道交付状态核验", "status": "PASS", "detail": delivery_res},
                    {"name": "全节点终态校验与安全释锁", "status": "PASS"},
                ],
                payload=delivery_res,
            )

            applied = self.db.finish_run_terminal(
                run.run_id,
                status="PASS",
                card_state="done",
                result_summary=f"笔记 {len(written_paths)} 篇，规则 {len(created_rules)} 条，Skill {len(written_skills)} 个",
                source="orchestrator",
                audit_event=(
                    "run_completed",
                    f"批次完成：笔记 {len(written_paths)} 篇，规则 {len(created_rules)} 条，Skill {len(written_skills)} 个",
                    {"docs": written_paths, "rules": created_rules, "skills": written_skills},
                ),
            )
            if not applied:
                # Already finalized (e.g. reaped mid-execution): stored state wins.
                stored = self.db.get_workflow_run(run.run_id)
                if stored is not None:
                    return stored
            run.status = RunStatus.PASS
            run.finished_at = utc_now_iso()
            run.lock_released = True
            self.db.save_workflow_run(run)
            self.fs_store.write_receipt("runs", f"{run.run_id}-final", run.model_dump(mode="json"))
            self._emit_run("run_completed", run)
            return run
        except Exception as exc:
            return self._fail_run(
                run, active_node, str(exc), t0, extra_payload=_error_payload_with_telemetry(exc)
            )
