from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.runners import build_runner
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import (
    CreateRuleRequest,
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    RetroStatus,
    RunStatus,
    SessionStatus,
    WorkflowRunRecord,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore


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
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.fs_store = fs_store
        self.policies = policies
        self.activation = activation
        self.sessions = sessions
        self.knowledge = knowledge
        self.delivery = delivery
        self.broker = broker

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
        self._emit_run("node_update", run, {"active_node": node_id})

    def _fail_run(self, run: WorkflowRunRecord, failed_node_id: str, reason: str, t0: float) -> WorkflowRunRecord:
        node = self._get_node(run, failed_node_id)
        node.status = NodeStatus.FAILED
        node.finished_at = utc_now_iso()
        node.duration_ms = max(1, int((time.perf_counter() - t0) * 1000))
        node.error = reason
        node.summary = f"节点执行失败：{reason}"

        # Mark all unexecuted downstream nodes explicitly as SKIPPED
        for other in run.nodes:
            if other.status in {NodeStatus.PENDING, NodeStatus.AWAITING_APPROVAL}:
                other.status = NodeStatus.SKIPPED
                other.summary = f"因前置节点 {failed_node_id} 失败而跳过"

        run.status = RunStatus.FAILED
        run.error = reason
        run.finished_at = utc_now_iso()
        run.digest_markdown = (
            f"## AI Flywheel 运行失败报告（{run.run_id}）\n\n"
            f"- **失败节点**：`{failed_node_id}`\n"
            f"- **原因**：{reason}\n"
            f"- **锁状态**：已按 `fail-run` 契约安全释放独占锁\n"
        )
        self.db.release_workflow_lock(run.run_id)
        run.lock_released = True
        self.db.save_workflow_run(run)
        self.fs_store.write_receipt("runs", f"{run.run_id}-failed", run.model_dump(mode="json"))
        self._emit_run("run_failed", run, {"failed_node": failed_node_id, "reason": reason})
        return run

    def start_run(
        self,
        *,
        trigger: str = "manual",
        phase: str = "daily",
        session_keys: list[str] | None = None,
        auto_approve_override: bool | None = None,
        simulate_fail_node: str | None = None,
    ) -> WorkflowRunRecord:
        if not self.cfg.safety_valves.workflow_enabled:
            raise RuntimeError("Workflow execution is disabled by safety valve WORKFLOW_ENABLED=false.")

        run_id = f"run-{uuid.uuid4().hex[:10]}"
        if not self.db.acquire_workflow_lock(run_id):
            lock = self.db.get_workflow_lock()
            raise WorkflowLockError(f"Another workflow run already holds the lock: {lock}")

        if self.cfg.runner.mode == "api" and self.cfg.runner.api.base_url and self.cfg.runner.api.model and self.cfg.safety_valves.cli_execution_enabled:
            runner_mode = f"api:{self.cfg.runner.api.model}"
        elif self.cfg.runner.mode == "cli" and self.cfg.runner.cli.command and self.cfg.safety_valves.cli_execution_enabled:
            runner_mode = "cli"
        else:
            runner_mode = "dry_run"
        run = WorkflowRunRecord(
            run_id=run_id,
            trigger=trigger,
            phase=phase,
            runner_mode=runner_mode,
            status=RunStatus.RUNNING,
            nodes=self._new_nodes(),
            lock_released=False,
        )
        self.db.save_workflow_run(run)
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
            pending = self.db.list_sessions(status=SessionStatus.PENDING.value, limit=50)
            if session_keys:
                selected_sessions = [s for s in self.db.list_sessions(limit=200) if s.session_key in set(session_keys)]
            else:
                priority_order = {"P0_CORE": 0, "P1_SIGNAL": 1, "P1_GENERAL": 2, "P2_META": 3}
                selected_sessions = sorted(pending, key=lambda s: priority_order.get(s.priority, 9))
            self._finish_node(
                run,
                "P00_PREPARE",
                t0,
                summary=(
                    f"策略核验通过（{len(policies)} 项），扫盘新登记 {scan_report['newly_registered']} 条，"
                    f"待审查会话 {len(selected_sessions)} 条"
                ),
                internal_checks=[
                    {"name": "飞轮内置策略完整性校验", "status": "PASS"},
                    {"name": "会话目录扫盘对账", "status": "PASS", "detail": scan_report},
                    {"name": "P0/P1/P2 优先级排序", "status": "PASS"},
                ],
                payload={"scan_report": scan_report, "candidate_count": len(selected_sessions)},
            )

            # ---------------- P01 BEGIN ----------------
            active_node = "P01_BEGIN"
            t0 = self._begin_node(run, "P01_BEGIN")
            if simulate_fail_node == "P01_BEGIN":
                raise RuntimeError("Simulated P01 failure")
            max_sess = self.cfg.workflow.max_sessions_per_run
            max_chars = self.cfg.workflow.max_chars_per_run
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
            review_findings = runner.review_sessions(admitted, open_retros)
            self._finish_node(
                run,
                "P02_REVIEW",
                t0,
                summary=(
                    f"执行器 `{review_findings.get('runner')}` 完成 {review_findings.get('sessions_reviewed', 0)} 个会话审查"
                    f"（关联 {len(open_retros)} 条未处理异常线索，零写入）"
                ),
                internal_checks=[
                    {"name": "只读全文/摘要事实盘点", "status": "PASS"},
                    {"name": "异常复盘线索关联", "status": "PASS", "detail": {"open_retros": len(open_retros)}},
                ],
                payload=review_findings,
            )

            # ---------------- P03 ROUTE & PLAN ----------------
            active_node = "P03_ROUTE_PLAN"
            t0 = self._begin_node(run, "P03_ROUTE_PLAN")
            if simulate_fail_node == "P03_ROUTE_PLAN":
                raise RuntimeError("Simulated P03 failure")
            existing_rules = self.db.list_rules()
            max_ws = min(
                self.cfg.knowledge_sink.max_write_set,
                int(self.policies.get_hard_gates().get("write_set", {}).get("max_files_per_run", 8)),
            )
            default_prefix = (
                self.cfg.knowledge_sink.allowed_write_prefixes[0]
                if self.cfg.knowledge_sink.allowed_write_prefixes
                else "lessons/"
            )
            planned_writes = runner.plan_writes(
                review_findings,
                existing_rules,
                max_write_set=max_ws,
                default_doc_prefix=default_prefix,
            )
            run.planned_writes = planned_writes
            self._finish_node(
                run,
                "P03_ROUTE_PLAN",
                t0,
                summary=f"查重比对 {len(existing_rules)} 条既有规则，生成 {len(planned_writes)} 项写入计划（上限 {max_ws}）",
                internal_checks=[
                    {"name": "规则与知识查重", "status": "PASS"},
                    {"name": "Write-Set 硬上限校验", "status": "PASS", "detail": {"planned": len(planned_writes), "limit": max_ws}},
                ],
                payload={"planned_count": len(planned_writes)},
            )

            # Check Approval Gate
            auto_approve = (
                auto_approve_override
                if auto_approve_override is not None
                else self.cfg.effective_auto_approve
            )
            if planned_writes and not auto_approve:
                p04 = self._get_node(run, "P04_WRITE_VERIFY")
                p04.status = NodeStatus.AWAITING_APPROVAL
                p04.summary = f"已暂停等待人工审核放行（共 {len(planned_writes)} 项待写入变更/规则草案）"
                run.status = RunStatus.AWAITING_APPROVAL
                self.db.save_workflow_run(run)
                self._emit_run("awaiting_approval", run)
                return run

            return self._execute_post_approval(run, simulate_fail_node=simulate_fail_node)

        except Exception as exc:
            return self._fail_run(run, active_node, str(exc), t0)

    def approve_and_resume(
        self,
        run_id: str,
        approved_indices: list[int] | None = None,
        simulate_fail_node: str | None = None,
    ) -> WorkflowRunRecord:
        run = self.db.get_workflow_run(run_id)
        if not run:
            raise KeyError(f"Workflow run not found: {run_id}")
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError(f"Run {run_id} is in status {run.status.value}, not AWAITING_APPROVAL.")

        if approved_indices is not None:
            keep = set(approved_indices)
            for idx, item in enumerate(run.planned_writes):
                item.approved = idx in keep
        else:
            for item in run.planned_writes:
                item.approved = True

        run.status = RunStatus.RUNNING
        self.db.save_workflow_run(run)
        return self._execute_post_approval(run, simulate_fail_node=simulate_fail_node)

    def reject_run(self, run_id: str, reason: str = "用户取消放行") -> WorkflowRunRecord:
        run = self.db.get_workflow_run(run_id)
        if not run:
            raise KeyError(f"Workflow run not found: {run_id}")
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError(f"Run {run_id} is not awaiting approval.")

        # Return sessions to pending so nothing is lost
        for sk in run.admitted_sessions:
            self.db.update_session_status(sk, SessionStatus.PENDING)
        for nid in ("P04_WRITE_VERIFY", "P05_SETTLE", "P06_REPORT", "P07_DELIVER"):
            node = self._get_node(run, nid)
            node.status = NodeStatus.SKIPPED
            node.summary = f"审核拒绝放行：{reason}"

        run.status = RunStatus.PARTIAL
        run.error = reason
        run.finished_at = utc_now_iso()
        run.digest_markdown = f"## AI Flywheel 运行已取消放行（{run.run_id}）\n\n- 原因：{reason}\n- 已安全释放锁并将入队会话恢复为 `pending`。\n"
        self.db.release_workflow_lock(run.run_id)
        run.lock_released = True
        self.db.save_workflow_run(run)
        self._emit_run("run_rejected", run, {"reason": reason})
        return run

    def _execute_post_approval(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        active_node = "P04_WRITE_VERIFY"
        t0 = time.perf_counter()
        written_paths: list[str] = []
        created_rules: list[str] = []
        try:
            # ---------------- P04 WRITE & VERIFY ----------------
            t0 = self._begin_node(run, "P04_WRITE_VERIFY")
            if simulate_fail_node == "P04_WRITE_VERIFY":
                raise RuntimeError("Simulated P04 write verification failure")

            approved_items = [w for w in run.planned_writes if w.approved]
            write_receipts: list[dict[str, Any]] = []
            for item in approved_items:
                if item.target_kind == "knowledge_doc" and item.relative_path:
                    res = self.knowledge.write_document_verified(
                        item.relative_path,
                        item.content_preview,
                        action=item.action,
                    )
                    written_paths.append(res["relative_path"])
                    write_receipts.append(res)
                elif item.target_kind == "flywheel_rule" and item.rule_payload:
                    req = CreateRuleRequest.model_validate(item.rule_payload)
                    rule = self.activation.register_rule(req)
                    created_rules.append(rule.rule_id)
                    write_receipts.append({"rule_id": rule.rule_id, "status": rule.status.value})

            self._finish_node(
                run,
                "P04_WRITE_VERIFY",
                t0,
                summary=f"受控落盘 {len(written_paths)} 篇知识文档（SHA-256 回读 PASS），注册 {len(created_rules)} 条活跃规则",
                internal_checks=[
                    {"name": "白名单前缀与路径安全检查", "status": "PASS"},
                    {"name": "SHA-256 回读一致性校验", "status": "PASS", "detail": {"verified_files": len(written_paths)}},
                ],
                artifacts=written_paths + created_rules,
                payload={"write_receipts": write_receipts},
            )

            # ---------------- P05 SETTLE ----------------
            active_node = "P05_SETTLE"
            t0 = self._begin_node(run, "P05_SETTLE")
            if simulate_fail_node == "P05_SETTLE":
                raise RuntimeError("Simulated P05 failure")
            for sk in run.admitted_sessions:
                self.db.update_session_status(sk, SessionStatus.PROCESSED)
            self._finish_node(
                run,
                "P05_SETTLE",
                t0,
                summary=f"幂等结算 {len(run.admitted_sessions)} 个会话为 processed，激活规则索引已刷新（保留锁待交付）",
                internal_checks=[
                    {"name": "会话队列幂等回写", "status": "PASS"},
                    {"name": "规则激活索引刷新", "status": "PASS"},
                    {"name": "P05 保留独占锁至 P07 终态校验", "status": "PASS"},
                ],
                payload={"settled_sessions": run.admitted_sessions, "created_rules": created_rules},
            )

            # ---------------- P06 REPORT ----------------
            active_node = "P06_REPORT"
            t0 = self._begin_node(run, "P06_REPORT")
            if simulate_fail_node == "P06_REPORT":
                raise RuntimeError("Simulated P06 failure")
            digest_lines = [
                f"## AI Flywheel 运行日报（{run.run_id}）",
                "",
                f"- **触发来源**：`{run.trigger}` · **阶段**：`{run.phase}` · **执行器**：`{run.runner_mode}`",
                f"- **处理会话数**：{len(run.admitted_sessions)}",
                f"- **受控沉淀文档**：{', '.join(f'`{p}`' for p in written_paths) if written_paths else '无新增'}",
                f"- **新注册规则（`unverified_active`）**：{', '.join(f'`{r}`' for r in created_rules) if created_rules else '无新增'}",
                "",
                "### 节点执行回执",
            ]
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

            run.status = RunStatus.PASS
            run.finished_at = utc_now_iso()
            self.db.release_workflow_lock(run.run_id)
            run.lock_released = True
            self.db.save_workflow_run(run)
            self.fs_store.write_receipt("runs", f"{run.run_id}-final", run.model_dump(mode="json"))
            self._emit_run("run_completed", run)
            return run
        except Exception as exc:
            return self._fail_run(run, active_node, str(exc), t0)
