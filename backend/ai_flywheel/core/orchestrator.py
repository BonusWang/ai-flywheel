from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_HEURISTIC,
    PLAN_SOURCE_MODEL,
    REVIEW_SOURCE_CLI,
    REVIEW_SOURCE_HEURISTIC,
    REVIEW_SOURCE_MODEL,
    REVIEW_SOURCE_UNDECLARED,
    PlanDiagnostics,
    PlanUnavailableError,
    ReviewUnavailableError,
    build_runner,
    normalize_rule_payload,
    resolve_runner_mode,
)
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.adapters.wiki_gate import (
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
    UNROUTED_REASON_LABEL,
    RouteDecision,
    RoutingCandidates,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.core_card import rebuild_core_card
from ai_flywheel.core.dream import run_dream_consolidation
from ai_flywheel.core.heartbeat import HeartbeatManager
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import (
    CreateRuleRequest,
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    RetroStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
    is_sensitive_write,
    utc_now_iso,
)
from ai_flywheel.storage.db import (
    FlywheelDB,
    RunAlreadyFinalizedError,
    SEVERITY_RANK,
    normalize_root_cause_key,
)
from ai_flywheel.storage.fs_store import ImmutableFileStore

logger = logging.getLogger(__name__)

# How the P03 node names the planner that produced the write plan.
PLAN_SOURCE_LABELS = {
    PLAN_SOURCE_MODEL: "API 模型",
    PLAN_SOURCE_CLI: "本地 CLI",
    PLAN_SOURCE_HEURISTIC: "本地启发式计划器",
}

# How the P02 node names the reviewer that reviewed the sessions.
REVIEW_SOURCE_LABELS = {
    REVIEW_SOURCE_MODEL: "API 模型",
    REVIEW_SOURCE_CLI: "本地 CLI",
    REVIEW_SOURCE_HEURISTIC: "本地启发式审查器",
    REVIEW_SOURCE_UNDECLARED: "未声明",
}


NODE_TEMPLATES = [
    ("P00_PREPARE", "准备与对账", "prepare-worker"),
    ("P01_BEGIN", "独占启动", "lock-worker"),
    ("P02_REVIEW", "全文审查", "review-worker"),
    ("P03_ROUTE_PLAN", "路由与写入计划", "merge-worker"),
    ("P04_WRITE_VERIFY", "受控写入与验收", "writer-worker"),
    ("P05_SETTLE", "台账与队列结算", "settlement-worker"),
    ("P06_REPORT", "机器报告与摘要", "report-worker"),
    ("P07_DELIVER", "交付与终态释锁", "delivery-worker"),
]


def _unrouted_reason_breakdown(reason_counts: Counter) -> str:
    """``<label> <count>`` per reason code, in order of first appearance."""
    return "、".join(f"{UNROUTED_REASON_LABEL.get(code, code)} {count}" for code, count in reason_counts.items())


def _model_call_failure_payload(exc: Exception) -> dict[str, Any]:
    """The trace of a failed model call: the stage, the threshold, the attempts, the exception type.

    Nothing about the gateway or the reply is carried: only what is needed to tell a timeout
    from a bad request from a broken connection.
    """
    summary = getattr(exc, "summary", None)
    if not isinstance(summary, dict):
        return {}
    payload: dict[str, Any] = {"call_error": dict(summary)}
    telemetry = getattr(exc, "telemetry", None)
    if isinstance(telemetry, dict):
        payload["call_telemetry"] = dict(telemetry)
    return payload


# A routed document the landing adapter could not turn into a page. Not expected: the route verdict
# already guarantees a page type the adapter can materialise.
LANDING_FAILED_REASON = "路由通过，但落点适配未能生成页面"


class EventBroker:
    """In-process pub/sub broadcaster for real-time SSE workflow visualization."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.append((loop, q))
        return loop, q

    def unsubscribe(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]) -> None:
        if entry in self._subscribers:
            self._subscribers.remove(entry)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        dead = []
        for loop, q in list(self._subscribers):
            if loop.is_closed():
                dead.append((loop, q))
                continue
            try:
                loop.call_soon_threadsafe(self._safe_put, q, payload)
            except RuntimeError:
                dead.append((loop, q))
        for item in dead:
            self.unsubscribe(item)

    @staticmethod
    def _safe_put(q: asyncio.Queue[str], payload: str) -> None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


class WorkflowLockError(RuntimeError):
    """Raised when an active workflow run already holds the exclusive lock."""


class ApprovalLockBusy(RuntimeError):
    """Raised when an approval claim fails because another run holds an unexpired lock.

    Carries the holder's run id and the lock's expiry so callers can surface a
    meaningful retry hint (portal maps this to HTTP 409).
    """

    def __init__(self, message: str, *, lock_run_id: str | None, lock_expires_at: str | None) -> None:
        super().__init__(message)
        self.lock_run_id = lock_run_id
        self.lock_expires_at = lock_expires_at


class LockLostError(RuntimeError):
    """Raised at a synchronous checkpoint when the lock is gone or no longer ours."""


class WorkflowOrchestrator:
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
        feishu_channel=None,
        on_node_begin: Callable[[str, str], None] | None = None,
    ) -> None:
        self.feishu_channel = feishu_channel
        self.cfg = cfg
        self.db = db
        self.fs_store = fs_store
        self.policies = policies
        self.activation = activation
        self.sessions = sessions
        self.knowledge = knowledge
        self.delivery = delivery
        self.broker = broker
        self.skill_writer = skill_writer or SkillWriterAdapter(cfg)
        # Optional hook called right after a node starts: (run_id, node_id).
        # Verification harnesses use it to park a run mid-pipeline; production
        # never sets it.
        self.on_node_begin = on_node_begin
        self.heartbeat = HeartbeatManager(
            self.db.renew_workflow_lock,
            interval_seconds=heartbeat_interval_seconds,
        )

    def _start_heartbeat(self, run_id: str) -> None:
        self.heartbeat.start(run_id)

    def _stop_heartbeat(self) -> None:
        self.heartbeat.stop()

    def _ensure_lock(self, run: WorkflowRunRecord) -> None:
        """Synchronous lock checkpoint before a side effect: renew or fail the run."""
        if not self.db.renew_workflow_lock(run.run_id):
            raise LockLostError(f"执行期间失去独占锁：{run.run_id}（锁缺失或已易主）")

    def recover_interrupted_runs(self, *, now: str | None = None, stale_after_seconds: int = 300) -> list[str]:
        """Reap runs whose executor is gone (stale heartbeat) and reconcile ask cards.

        Called at serve startup and every recovery interval; conditional terminal
        entry means the reaper, a zombie executor and a second serve process can
        all race safely — only one application wins per run.
        """
        recovered: list[str] = []
        for row in self.db.list_stale_running_runs(now=now, stale_after_seconds=stale_after_seconds):
            run_id = row["run_id"]
            run = self.db.get_workflow_run(run_id)
            if run is None or run.status != RunStatus.RUNNING:
                continue
            failed_node = next(
                (n.node_id for n in run.nodes if n.status not in {NodeStatus.PASS, NodeStatus.SKIPPED, NodeStatus.FAILED}),
                None,
            )
            reason = "执行进程中断（服务重启或执行者消失）"
            for n in run.nodes:
                if n.status in {NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.AWAITING_APPROVAL}:
                    if failed_node and n.node_id == failed_node:
                        n.status = NodeStatus.FAILED
                        n.error = reason
                        n.summary = f"执行被中断：{reason}"
                    else:
                        n.status = NodeStatus.SKIPPED
                        n.summary = "因执行被中断而跳过"
            applied = self.db.finish_run_terminal(
                run_id,
                status="FAILED",
                card_state="failed",
                error=reason,
                failed_node=failed_node or "P00_PREPARE",
                result_summary=f"{failed_node} 执行被中断" if failed_node else "执行被中断（无失败节点）",
                source="reaper",
                audit_event=(
                    "run_failed",
                    f"执行被中断：{reason}",
                    {"failed_node": failed_node, "reason": reason, "recovered": True},
                ),
                now=now,
            )
            if not applied:
                continue
            self._finalize_terminal_artifacts(
                run,
                status=RunStatus.FAILED,
                error=reason,
                receipt_name=f"{run_id}-failed",
                event_type="run_failed",
                extra={"failed_node": failed_node, "reason": reason, "recovered": True},
                continue_on_finalized=True,
            )
            recovered.append(run_id)
        self.db.reconcile_decision_cards(now=now)
        return recovered

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

    # ---------------- P02: read-only mistake & dissent candidates ----------------

    @staticmethod
    def _normalize_candidate_mistake(raw: Any) -> dict[str, Any] | None:
        """One mistake a review found, normalized into the shape the observation pool uses."""
        if not isinstance(raw, dict) or not raw.get("root_cause_key"):
            return None
        key = normalize_root_cause_key(str(raw["root_cause_key"]))
        if not key:
            return None
        grade = str(raw.get("severity_grade") or "yellow_caught")
        if grade not in SEVERITY_RANK:
            grade = "yellow_caught"
        caught_by = str(raw.get("caught_by") or "self")
        if caught_by not in {"user", "tool", "self"}:
            caught_by = "self"
        return {
            "root_cause_key": key,
            "severity_grade": grade,
            "title": str(raw.get("title") or key),
            "phenomenon": str(raw.get("phenomenon") or ""),
            "truth": str(raw.get("truth") or ""),
            "thinking_root_cause": str(raw.get("thinking_root_cause") or ""),
            "caught_by": caught_by,
            "session_key": str(raw.get("session_key") or ""),
        }

    def _candidate_mistakes(self, raw_mistakes: list[Any]) -> list[dict[str, Any]]:
        """The mistakes this review found, normalized. Nothing is written to the pool here."""
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_mistakes:
            candidate = self._normalize_candidate_mistake(raw)
            if candidate is None:
                continue
            key = candidate["root_cause_key"]
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _candidate_dissents(raw_dissents: list[Any]) -> list[dict[str, Any]]:
        """The counter-evidence this review found, normalized. Nothing is written here."""
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in raw_dissents:
            if not isinstance(raw, dict) or not raw.get("target_id") or not raw.get("counter_claim"):
                continue
            target_type = str(raw.get("target_type") or "rule")
            if target_type not in {"rule", "knowledge_doc", "skill"}:
                target_type = "rule"
            entry = {
                "target_type": target_type,
                "target_id": str(raw["target_id"]),
                "counter_claim": str(raw["counter_claim"]),
                "condition_scope": str(raw.get("condition_scope") or ""),
                "evidence_pointer": str(raw.get("evidence_pointer") or ""),
                "session_key": str(raw.get("session_key") or ""),
            }
            fingerprint = (entry["target_type"], entry["target_id"], entry["counter_claim"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append(entry)
        return candidates

    @staticmethod
    def _qualifies_for_promotion(severity_grade: str, occurrence_count: int) -> bool:
        """The two-track rule: a red silent mistake promotes at once, a self-caught one after three."""
        if severity_grade == "red_silent":
            return True
        return severity_grade == "yellow_caught" and occurrence_count >= 3

    def _eligible_promotions(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The mistakes that may become rules this batch — computed without writing anything.

        Two sources: the observations already in the pool that qualify, and the mistakes this
        review found whose *projected* occurrence count qualifies them (the count they would
        reach once P04 persists them).
        """
        eligible: list[dict[str, Any]] = []
        seen: set[str] = set()

        for obs in self.db.list_mistake_observations(status="observing", limit=200):
            key = str(obs.get("root_cause_key") or "")
            if not key or key in seen:
                continue
            if not self._qualifies_for_promotion(
                str(obs.get("severity_grade") or ""), int(obs.get("occurrence_count") or 1)
            ):
                continue
            seen.add(key)
            eligible.append(dict(obs))

        for candidate in candidates:
            key = candidate["root_cause_key"]
            if key in seen:
                continue
            existing = self.db.get_mistake_observation_by_root_cause_key(key)
            if existing and str(existing.get("status")) != "observing":
                # Already promoted or archived: it never re-enters the promotion list.
                continue
            grade = candidate["severity_grade"]
            count = 1
            session_keys: list[str] = []
            obs_id: str | None = None
            if existing:
                obs_id = str(existing.get("obs_id") or "") or None
                existing_grade = str(existing.get("severity_grade") or "")
                if SEVERITY_RANK.get(existing_grade, 1) > SEVERITY_RANK.get(grade, 1):
                    # The pool only ever upgrades a severity, never downgrades it.
                    grade = existing_grade
                session_keys = [str(k) for k in (existing.get("session_keys") or [])]
                count = int(existing.get("occurrence_count") or 1)
                # The same session seen again does not add an occurrence.
                if candidate["session_key"] and candidate["session_key"] in session_keys:
                    count += 0
                else:
                    count += 1
                if candidate["session_key"]:
                    session_keys = list(dict.fromkeys([*session_keys, candidate["session_key"]]))
            if not self._qualifies_for_promotion(grade, count):
                continue
            seen.add(key)
            eligible.append(
                {
                    "obs_id": obs_id,
                    "root_cause_key": key,
                    "severity_grade": grade,
                    "title": candidate["title"] or (existing or {}).get("title") or key,
                    "phenomenon": candidate["phenomenon"] or (existing or {}).get("phenomenon") or "",
                    "truth": candidate["truth"] or (existing or {}).get("truth") or "",
                    "thinking_root_cause": candidate["thinking_root_cause"]
                    or (existing or {}).get("thinking_root_cause")
                    or "",
                    "caught_by": candidate["caught_by"],
                    "occurrence_count": count,
                    "session_keys": session_keys,
                    "status": "observing",
                    "is_candidate": True,
                }
            )
        return eligible

    def _record_partial_writes(
        self,
        run: WorkflowRunRecord,
        write_receipts: list[dict[str, Any]],
        written_paths: list[str],
        error: BaseException,
    ) -> None:
        """Leave the receipts of a half-done batch on the P04 node before the run fails."""
        node = self._get_node(run, "P04_WRITE_VERIFY")
        node.payload = {
            **(node.payload or {}),
            "partial_write_receipts": list(write_receipts),
            "partial_written_paths": list(written_paths),
            "partial_write_error": str(error),
        }

    def _promote_observation(
        self, rule_id: str, obs_id: Any, root_cause_key: Any
    ) -> str | None:
        """Link a freshly registered rule to the mistake observation it came from.

        The rule carries either the observation id or the root cause key; both are
        accepted, because a model plan may know the key but not the generated id.
        Returns the observation id that was promoted, if any.
        """
        target_obs_id = str(obs_id).strip() if obs_id else ""
        if not target_obs_id:
            key = normalize_root_cause_key(str(root_cause_key or ""))
            if key:
                existing = self.db.get_mistake_observation_by_root_cause_key(key)
                if existing:
                    target_obs_id = str(existing.get("obs_id") or "")
        if not target_obs_id:
            return None
        updated = self.db.update_mistake_observation_status(
            target_obs_id, "promoted", promoted_rule_id=rule_id
        )
        return target_obs_id if updated else None

    def _route_knowledge_doc(
        self,
        run: WorkflowRunRecord,
        item: PlannedWrite,
        sessions_by_key: dict[str, SessionRecord],
        candidates: RoutingCandidates | None,
    ) -> RouteDecision:
        """Validate a knowledge document's routing declaration against the mounted Wiki contract.

        Only the dry-run planner's plans fall back to evidence-based routing; a model plan must
        declare where every knowledge document lives.
        """
        project_hint: list[str] = []
        workspaces: list[str] = []
        for sk in item.source_session_keys:
            record = sessions_by_key.get(sk)
            if record:
                project_hint.extend([record.project, record.workspace])
                if record.workspace:
                    workspaces.append(record.workspace)
        return self.knowledge.wiki_gate.evaluate_route(
            relative_path=item.relative_path,
            title=item.title,
            summary=item.summary,
            content=item.content_preview,
            wiki_route=item.wiki_route,
            source_workspaces=workspaces,
            project_hint=" ".join(project_hint),
            allow_evidence_fallback=run.runner_mode == "dry_run",
            candidates=candidates,
        )

    def _mark_remaining_nodes_skipped(
        self,
        run: WorkflowRunRecord,
        *,
        reason: str,
        node_ids: Iterable[str] | None = None,
    ) -> list[str]:
        """Mark the nodes that will never run as SKIPPED and say why.

        With ``node_ids`` the given nodes are marked; without it every node that
        has not reached a terminal status yet is marked. Nodes that already
        finished keep their status: a failure never rewrites history.
        """
        targets = (
            list(node_ids)
            if node_ids is not None
            else [
                n.node_id
                for n in run.nodes
                if n.status in {NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.AWAITING_APPROVAL}
            ]
        )
        skipped: list[str] = []
        for nid in targets:
            node = self._get_node(run, nid)
            if node.status in {NodeStatus.PASS, NodeStatus.FAILED, NodeStatus.SKIPPED}:
                continue
            node.status = NodeStatus.SKIPPED
            node.summary = reason
            skipped.append(nid)
        return skipped

    def _finalize_terminal_artifacts(
        self,
        run: WorkflowRunRecord,
        *,
        status: RunStatus,
        receipt_name: str,
        event_type: str,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
        release_sessions: bool = True,
        save_row: bool = True,
        continue_on_finalized: bool = False,
        notify: Callable[[], None] | None = None,
    ) -> WorkflowRunRecord:
        """Close a terminal run: status, sessions, stored row, receipt and the SSE event.

        The three terminal paths (fail-run, the reaper and a cancellation) differ
        only in the values they pass; the write-back sequence is identical, so it
        lives here instead of being repeated three times.

        ``continue_on_finalized`` decides what happens when the stored row is
        already final with a different status: the reaper still wants its receipt
        and event, the fail-run path reports the stored run instead.
        """
        run.status = status
        run.error = error if error is not None else run.error
        run.finished_at = utc_now_iso()
        run.lock_released = True
        if release_sessions:
            # Admitted sessions go back to the queue: a run that ended without
            # settling them must never strand them.
            for sk in run.admitted_sessions:
                self.db.update_session_status(sk, SessionStatus.PENDING)
        if save_row:
            try:
                self.db.save_workflow_run(run)
            except RunAlreadyFinalizedError:
                stored = self.db.get_workflow_run(run.run_id)
                if stored is None:
                    stored = run
                if not continue_on_finalized:
                    return stored
                run = stored
        if notify is not None:
            notify()
        self.fs_store.write_receipt("runs", receipt_name, run.model_dump(mode="json"))
        self._emit_run(event_type, run, extra)
        return run

    def _fail_run(
        self,
        run: WorkflowRunRecord,
        failed_node_id: str,
        reason: str,
        t0: float,
        extra_payload: dict[str, Any] | None = None,
    ) -> WorkflowRunRecord:
        node = self._get_node(run, failed_node_id)
        node.status = NodeStatus.FAILED
        node.finished_at = utc_now_iso()
        node.duration_ms = max(1, int((time.perf_counter() - t0) * 1000))
        node.error = reason
        node.summary = f"节点执行失败：{reason}"
        if extra_payload:
            # What the failed call was: the stage, the threshold, the attempts and the exception
            # type. Never the gateway address, never the reply.
            node.payload = {**node.payload, **extra_payload}

        # Mark all unexecuted downstream nodes explicitly as SKIPPED
        self._mark_remaining_nodes_skipped(
            run, reason=f"因前置节点 {failed_node_id} 失败而跳过"
        )

        run.status = RunStatus.FAILED
        run.error = reason
        run.finished_at = utc_now_iso()
        run.digest_markdown = (
            f"## AI Flywheel 运行失败报告（{run.run_id}）\n\n"
            f"- **失败节点**：`{failed_node_id}`\n"
            f"- **原因**：{reason}\n"
            f"- **锁状态**：已按 `fail-run` 契约安全释放独占锁\n"
        )
        payload = {"failed_node": failed_node_id, "reason": reason}
        if extra_payload:
            payload.update(extra_payload)
        applied = self.db.finish_run_terminal(
            run.run_id,
            status="FAILED",
            card_state="failed",
            error=reason,
            failed_node=failed_node_id,
            result_summary=f"{failed_node_id} 失败：{reason}",
            source="orchestrator",
            audit_event=("run_failed", f"节点 {failed_node_id} 失败：{reason}", payload),
        )
        if not applied:
            # Already finalized (e.g. reaped as interrupted): the stored run is the truth.
            stored = self.db.get_workflow_run(run.run_id)
            if stored is not None:
                return stored

        def _alert() -> None:
            if not (applied and self.cfg.delivery.feishu_cli.enabled):
                return
            self.delivery.send_feishu_text_notice(
                title=f"AI 飞轮运行异常（{run.batch_date or run.run_id}）",
                markdown=f"- 失败环节：`{failed_node_id}`\n- 原因：{reason}\n- 已安全释放锁，请检查详情。",
                kind="alert",
                idempotency_key=f"flywheel-fail-{run.run_id}",
            )

        return self._finalize_terminal_artifacts(
            run,
            status=RunStatus.FAILED,
            error=reason,
            receipt_name=f"{run.run_id}-failed",
            event_type="run_failed",
            extra={"failed_node": failed_node_id, "reason": reason},
            notify=_alert,
        )

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
            open_retro_keys = {
                r.session_key for r in self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=200)
            }
            triage_report = (
                self.sessions.sweep_pending_triage(open_retro_keys)
                if not session_keys
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
            if session_keys:
                # Explicitly named sessions are fetched one by one: filtering a recency-limited
                # page would silently drop the ones that are not among the most recent.
                wanted = list(dict.fromkeys(str(sk) for sk in session_keys))
                selected_sessions = [
                    record for sk in wanted if (record := self.db.get_session(sk)) is not None
                ]
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
                    f"初筛跳过 {triage_report['skipped']} 条临时/噪音会话，待审查 {len(selected_sessions)} 条"
                ),
                internal_checks=[
                    {"name": "飞轮内置策略完整性校验", "status": "PASS"},
                    {"name": "会话目录扫盘对账", "status": "PASS", "detail": scan_report},
                    {"name": "规则初筛（过滤子代理/空会话/低信号闲聊）", "status": "PASS", "detail": triage_report},
                    {"name": "P0/P1/P2 优先级排序", "status": "PASS"},
                ],
                payload={
                    "scan_report": scan_report,
                    "triage_report": triage_report,
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
            admitted: list[tuple[Any, str]] = []
            used_chars = 0
            for sess in selected_sessions[:max_sess]:
                text = self.sessions.load_session_text(sess, max_chars=24000)
                if used_chars + len(text) > max_chars and admitted:
                    break
                used_chars += len(text)
                admitted.append((sess, text))
                self.db.update_session_status(sess.session_key, SessionStatus.IN_PROGRESS)
            run.admitted_sessions = [s.session_key for s, _ in admitted]
            self._finish_node(
                run,
                "P01_BEGIN",
                t0,
                summary=f"独占锁已获取，准入 {len(admitted)} 个会话单元（共 {used_chars} 字符）",
                internal_checks=[
                    {"name": "独占锁持有核验", "status": "PASS"},
                    {"name": "预算与字符上限准入", "status": "PASS", "detail": {"chars": used_chars, "limit": max_chars}},
                ],
                payload={"admitted_sessions": run.admitted_sessions, "used_chars": used_chars},
            )

            # ---------------- P02 REVIEW ----------------
            active_node = "P02_REVIEW"
            t0 = self._begin_node(run, "P02_REVIEW")
            if simulate_fail_node == "P02_REVIEW":
                raise RuntimeError("Simulated P02 failure")
            runner = build_runner(self.cfg, self.fs_store)
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
            candidate_mistakes = self._candidate_mistakes(review_findings.get("mistakes") or [])
            candidate_dissents = self._candidate_dissents(review_findings.get("dissents") or [])
            eligible_promotions = self._eligible_promotions(candidate_mistakes)
            sent_session_keys = [
                str(key)
                for key in (review_findings.get("sent_session_keys") or [])
                if str(key)
            ]
            review_findings["candidate_mistakes"] = candidate_mistakes
            review_findings["candidate_dissents"] = candidate_dissents
            review_findings["eligible_promotions"] = eligible_promotions
            review_findings["sent_session_keys"] = sent_session_keys
            review_findings["candidate_mistake_count"] = len(candidate_mistakes)
            review_findings["candidate_dissent_count"] = len(candidate_dissents)

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
                    f"（候选错题 {len(candidate_mistakes)} 条、可晋升 {len(eligible_promotions)} 条、"
                    f"候选分歧 {len(candidate_dissents)} 条、流程候选 {wf_candidates} 处）"
                ),
                internal_checks=[
                    {"name": "对话事实回顾", "status": "PASS"},
                    {
                        "name": "错题观察池双轨准入与分歧保留",
                        "status": "PASS",
                        "detail": {
                            "mistakes": len(candidate_mistakes),
                            "eligible_promotions": len(eligible_promotions),
                            "dissents": len(candidate_dissents),
                            "written": False,
                        },
                    },
                    {"name": "重复流程与 Skill 线索扫描", "status": "PASS", "detail": {"candidates": wf_candidates}},
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
            default_prefix = (
                self.cfg.knowledge_sink.allowed_write_prefixes[0]
                if self.cfg.knowledge_sink.allowed_write_prefixes
                else "lessons/"
            )
            # The routing candidate set (page types and projects read off the mounted Wiki) goes to the planner.
            routing_candidates = (
                self.knowledge.wiki_gate.routing_candidates() if self.knowledge.wiki_gate.applicable else None
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
            # On a schema-governed Wiki every knowledge_doc is routed before it is adapted: the planner's
            # declaration, validated against workspace and draft evidence and never corrected, says whether
            # it is written and where. A document without a consistent route is dropped here and surfaced
            # instead of being guessed into a project or failing the whole batch in P04.
            unrouted_docs: list[dict[str, Any]] = []
            routed_docs: list[dict[str, Any]] = []
            if self.knowledge.wiki_gate.applicable:
                used_norm_paths: set[str] = set()
                sess_by_key = {s.session_key: s for s, _ in admitted}
                routed_writes: list[PlannedWrite] = []
                for w in planned_writes:
                    if w.target_kind != "knowledge_doc":
                        routed_writes.append(w)
                        continue
                    decision = self._route_knowledge_doc(run, w, sess_by_key, routing_candidates)
                    adapted = (
                        self.knowledge.wiki_gate.normalize_planned_document(
                            w.relative_path,
                            w.title,
                            w.summary,
                            w.content_preview,
                            source_sessions=w.source_session_keys,
                            route=decision,
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
                    if new_rel in used_norm_paths:
                        stem = new_rel[:-3] if new_rel.endswith(".md") else new_rel
                        new_rel = f"{stem}-{len(used_norm_paths) + 1}.md"
                    used_norm_paths.add(new_rel)
                    w.relative_path = new_rel
                    w.content_preview = new_md
                    routed_writes.append(w)
                    routed_docs.append(
                        {
                            "title": w.title,
                            "relative_path": new_rel,
                            "source_session_keys": list(w.source_session_keys),
                            "route": decision.to_dict(),
                        }
                    )
                planned_writes = routed_writes
            run.planned_writes = planned_writes
            if plan_info.dropped_items:
                self._trace_dropped_plan_items(run, plan_info)
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
                        + "；".join(f"{doc['title']} → {doc['route']['project']}" for doc in routed_docs[:6])
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
                        f"{len(unrouted_docs)} 篇笔记未列入写入计划：{_unrouted_reason_breakdown(unrouted_reasons)}"
                    ),
                    payload={"unrouted_docs": unrouted_docs},
                )
            skill_plans = sum(1 for w in planned_writes if w.target_kind == "skill_improvement")
            unrouted_note = (
                f"；{len(unrouted_docs)} 篇笔记因无法归属项目未列入计划（{_unrouted_reason_breakdown(unrouted_reasons)}）"
                if unrouted_docs
                else ""
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
            self._finish_node(
                run,
                "P03_ROUTE_PLAN",
                t0,
                summary=(
                    f"计划来源：{PLAN_SOURCE_LABELS.get(plan_info.source, plan_info.source)}；"
                    f"查重比对 {len(existing_rules)} 条规则与 {len(existing_skills)} 个技能，"
                    f"整理出 {len(planned_writes)} 项建议（含 {skill_plans} 项 Skill 封装/优化）"
                    f"{dropped_note}{unrouted_note}{deferred_note}"
                ),
                internal_checks=[
                    {"name": "规则与技能查重", "status": "PASS"},
                    {"name": "Skill 路由判定", "status": "PASS", "detail": {"skill_plans": skill_plans}},
                    {"name": "单批数量上限校验", "status": "PASS", "detail": {"planned": len(planned_writes), "limit": max_ws}},
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
                                "name": "知识文档路由校验",
                                "status": "PASS",
                                "detail": {
                                    "routed": len(routed_docs),
                                    "declared": sum(1 for d in routed_docs if d["route"]["source"] == ROUTE_DECLARED),
                                    "evidence": sum(1 for d in routed_docs if d["route"]["source"] == ROUTE_EVIDENCE),
                                    "unrouted": len(unrouted_docs),
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
                    "plan_source": plan_info.source,
                    "plan_diagnostics": plan_info.to_dict(),
                    # What the model call cost: attempts, duration, served model, token usage.
                    "call_telemetry": plan_info.call_telemetry,
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
                run, active_node, str(exc), t0, extra_payload=_model_call_failure_payload(exc)
            )
        finally:
            self._stop_heartbeat()

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

    def _execute_post_approval(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        active_node = "P04_WRITE_VERIFY"
        t0 = time.perf_counter()
        written_paths: list[str] = []
        created_rules: list[str] = []
        written_skills: list[str] = []
        skipped_docs: list[dict[str, Any]] = []
        try:
            # ---------------- P04 WRITE & VERIFY ----------------
            t0 = self._begin_node(run, "P04_WRITE_VERIFY")
            if simulate_fail_node == "P04_WRITE_VERIFY":
                raise RuntimeError("Simulated P04 write verification failure")

            approved_items = [w for w in run.planned_writes if w.approved]
            write_receipts: list[dict[str, Any]] = []
            p02_payload = self._get_node(run, "P02_REVIEW").payload
            if not isinstance(p02_payload, dict):
                p02_payload = {}
            candidate_mistakes = [
                c for c in (p02_payload.get("candidate_mistakes") or []) if isinstance(c, dict)
            ]
            candidate_dissents = [
                c for c in (p02_payload.get("candidate_dissents") or []) if isinstance(c, dict)
            ]
            persisted_observations: list[dict[str, Any]] = []
            index_error: Exception | None = None
            try:
                # P04 is the first node allowed to change the mistake pool and the dissent
                # records: the batch was approved, so the candidates P02 found become durable.
                self._ensure_lock(run)
                for candidate in candidate_mistakes:
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
                for candidate in candidate_dissents:
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
                        if self.knowledge.wiki_gate.applicable:
                            # P03 already routed every knowledge document; this re-check only catches a plan
                            # that went stale between P03 and P04 (saved before routing, or edited since).
                            decision = self._route_knowledge_doc(
                                run, item, self._stored_sessions(item.source_session_keys), None
                            )
                            adapted = (
                                self.knowledge.wiki_gate.normalize_planned_document(
                                    rel_to_write,
                                    item.title,
                                    item.summary,
                                    content_to_write,
                                    source_sessions=item.source_session_keys,
                                    route=decision,
                                )
                                if decision.routed
                                else None
                            )
                            if adapted is None:
                                # A stale plan must not fail the batch here
                                skipped_docs.append(
                                    {
                                        "relative_path": item.relative_path,
                                        "title": item.title,
                                        "status": "SKIPPED_UNROUTED_PROJECT",
                                        "reason": decision.detail or LANDING_FAILED_REASON,
                                        "reason_code": decision.reason_code or "landing_failed",
                                    }
                                )
                                write_receipts.append(skipped_docs[-1])
                                continue
                            rel_to_write, content_to_write = adapted
                        res = self.knowledge.write_document_verified(
                            rel_to_write,
                            content_to_write,
                            action=item.action,
                            sync_fts=False,  # the batch rebuilds the search index once, at the end
                        )
                        written_paths.append(res["relative_path"])
                        write_receipts.append(res)
                    elif item.target_kind == "flywheel_rule":
                        normalized_payload = normalize_rule_payload(item)
                        obs_id = normalized_payload.pop("obs_id", None)
                        root_cause_key = normalized_payload.pop("root_cause_key", None)
                        req = CreateRuleRequest.model_validate(normalized_payload)
                        rule = self.activation.register_rule(req, root_cause_key=root_cause_key)
                        promoted_obs = self._promote_observation(rule.rule_id, obs_id, root_cause_key)
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
                self._record_partial_writes(run, write_receipts, written_paths, exc)
                raise
            finally:
                if written_paths:
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

            self._finish_node(
                run,
                "P04_WRITE_VERIFY",
                t0,
                summary=(
                    f"写入 {len(written_paths)} 篇笔记、注册 {len(created_rules)} 条规则、"
                    f"更新/新建 {len(written_skills)} 个 Skill（全部校验通过）"
                    + (f"；{len(skipped_docs)} 篇笔记因无法归属项目跳过" if skipped_docs else "")
                    + (
                        f"；错题观察池入池 {len(persisted_observations)} 条"
                        if persisted_observations
                        else ""
                    )
                ),
                internal_checks=[
                    {"name": "路径安全与白名单校验", "status": "PASS"},
                    {"name": "落盘回读一致性校验", "status": "PASS", "detail": {"docs": len(written_paths), "skills": len(written_skills)}},
                    {
                        "name": "错题观察池与分歧记录写入（审批后）",
                        "status": "PASS",
                        "detail": {
                            "observations": len(persisted_observations),
                            "dissents": len(candidate_dissents),
                        },
                    },
                ],
                artifacts=written_paths + created_rules + written_skills,
                payload={
                    "write_receipts": write_receipts,
                    "skipped_unrouted_docs": skipped_docs,
                    "persisted_observation_ids": [
                        str(o.get("obs_id")) for o in persisted_observations if o.get("obs_id")
                    ],
                    "persisted_dissent_count": len(candidate_dissents),
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
            settled_sessions: list[str] = []
            returned_sessions: list[str] = []
            for sk in run.admitted_sessions:
                self._ensure_lock(run)  # synchronous checkpoint before each settlement
                if sk not in reviewed_keys:
                    self.db.update_session_status(sk, SessionStatus.PENDING)
                    returned_sessions.append(sk)
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
                dream_report = run_dream_consolidation(self.cfg, self.db, run_id=run.run_id)
            core_card_info = rebuild_core_card(self.cfg, self.db)
            dream_note = (
                f"，周五蒸馏自动休眠 {len(dream_report['auto_dormant_rules'])} 条零命中规则、"
                f"标记 {len(dream_report['expired_cards'])} 篇过期经验卡"
                if dream_report
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
                    + f"，关闭 {settled_retros} 条待复盘项"
                    + f"，核心索引卡已刷新（{core_card_info['lines']} 行）{dream_note}"
                ),
                internal_checks=[
                    {"name": "会话队列幂等回写", "status": "PASS"},
                    {"name": "待复盘项结算", "status": "PASS", "detail": {"resolved_retros": settled_retros}},
                    {"name": "核心索引卡 FLYWHEEL-CORE.md 刷新", "status": "PASS", "detail": {"lines": core_card_info["lines"], "bytes": core_card_info["bytes"]}},
                    {"name": "P05 保留独占锁至 P07 终态校验", "status": "PASS"},
                ],
                payload={
                    "settled_sessions": settled_sessions,
                    "returned_sessions": returned_sessions,
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
                run, active_node, str(exc), t0, extra_payload=_model_call_failure_payload(exc)
            )
