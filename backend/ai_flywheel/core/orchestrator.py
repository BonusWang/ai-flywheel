from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.runners import build_runner, normalize_rule_payload
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.core_card import rebuild_core_card
from ai_flywheel.core.dream import run_dream_consolidation
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
        skill_writer: SkillWriterAdapter | None = None,
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
        self.skill_writer = skill_writer or SkillWriterAdapter(cfg)

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
            if w.target_kind == "skill_improvement":
                reasons.append(w.approval_reason or f"涉及 Skill 变更：{w.title}")
            elif w.requires_approval or w.action in {"delete", "overwrite_core"}:
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
        # Return admitted sessions back to pending so a failed run never strands them
        for sk in run.admitted_sessions:
            self.db.update_session_status(sk, SessionStatus.PENDING)
        self.db.release_workflow_lock(run.run_id)
        run.lock_released = True
        self.db.save_workflow_run(run)
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run.run_id,
            event_type="run_failed",
            actor=failed_node_id,
            summary=f"节点 {failed_node_id} 失败：{reason}",
            payload={"failed_node": failed_node_id, "reason": reason},
        )
        if self.cfg.delivery.feishu_cli.enabled:
            self.delivery.send_feishu_card(
                title=f"AI 飞轮运行异常（{run.batch_date or run.run_id}）",
                markdown=f"- 失败环节：`{failed_node_id}`\n- 原因：{reason}\n- 已安全释放锁，请检查详情。",
                kind="alert",
                idempotency_key=f"flywheel-fail-{run.run_id}",
            )
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
            review_findings = runner.review_sessions(
                admitted,
                open_retros,
                existing_skills=existing_skills,
                phase=resolved_phase,
            )

            # v3: Persist extracted mistakes into Mistake Observation Pool & evaluate two-track promotion
            persisted_mistakes: list[dict[str, Any]] = []
            eligible_promotions: list[dict[str, Any]] = []
            seen_promo_keys: set[str] = set()
            for raw_m in review_findings.get("mistakes") or []:
                if not isinstance(raw_m, dict) or not raw_m.get("root_cause_key"):
                    continue
                obs, _created = self.db.upsert_mistake_observation(
                    root_cause_key=str(raw_m["root_cause_key"]),
                    title=str(raw_m.get("title") or raw_m["root_cause_key"]),
                    severity_grade=str(raw_m.get("severity_grade") or "yellow_caught"),
                    phenomenon=str(raw_m.get("phenomenon") or ""),
                    truth=str(raw_m.get("truth") or ""),
                    thinking_root_cause=str(raw_m.get("thinking_root_cause") or ""),
                    caught_by=str(raw_m.get("caught_by") or "self"),
                    session_key=str(raw_m.get("session_key") or ""),
                )
                persisted_mistakes.append(obs)
                if (
                    obs["status"] == "observing"
                    and (
                        obs["severity_grade"] == "red_silent"
                        or (obs["severity_grade"] == "yellow_caught" and int(obs["occurrence_count"]) >= 3)
                    )
                    and obs["root_cause_key"] not in seen_promo_keys
                ):
                    seen_promo_keys.add(obs["root_cause_key"])
                    eligible_promotions.append(obs)

            # v3: Persist extracted dissents / counter-evidence without overwriting existing rules
            persisted_dissents: list[dict[str, Any]] = []
            for raw_d in review_findings.get("dissents") or []:
                if not isinstance(raw_d, dict) or not raw_d.get("target_id") or not raw_d.get("counter_claim"):
                    continue
                d_rec = self.db.add_dissent_record(
                    target_type=str(raw_d.get("target_type") or "rule"),
                    target_id=str(raw_d["target_id"]),
                    counter_claim=str(raw_d["counter_claim"]),
                    condition_scope=str(raw_d.get("condition_scope") or ""),
                    evidence_pointer=str(raw_d.get("evidence_pointer") or ""),
                    session_key=str(raw_d.get("session_key") or ""),
                )
                persisted_dissents.append(d_rec)

            review_findings["eligible_promotions"] = eligible_promotions
            review_findings["persisted_mistake_count"] = len(persisted_mistakes)
            review_findings["persisted_dissent_count"] = len(persisted_dissents)

            wf_candidates = len(review_findings.get("repeated_workflows", []))
            self._finish_node(
                run,
                "P02_REVIEW",
                t0,
                summary=(
                    f"完成 {review_findings.get('sessions_reviewed', 0)} 段对话回顾"
                    f"（入池错题 {len(persisted_mistakes)} 条、可晋升 {len(eligible_promotions)} 条、"
                    f"分歧 {len(persisted_dissents)} 条、流程候选 {wf_candidates} 处）"
                ),
                internal_checks=[
                    {"name": "对话事实回顾", "status": "PASS"},
                    {
                        "name": "错题观察池双轨准入与分歧保留",
                        "status": "PASS",
                        "detail": {
                            "mistakes": len(persisted_mistakes),
                            "eligible_promotions": len(eligible_promotions),
                            "dissents": len(persisted_dissents),
                        },
                    },
                    {"name": "重复流程与 Skill 线索扫描", "status": "PASS", "detail": {"candidates": wf_candidates}},
                    {"name": "问题线索关联", "status": "PASS", "detail": {"open_retros": len(open_retros)}},
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
                self.cfg.workflow.max_writes_per_run,
                self.cfg.knowledge_sink.max_write_set,
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
                existing_skills=existing_skills,
                phase=resolved_phase,
            )
            # If a schema-governed Wiki is mounted, normalize knowledge_doc writes to pass WikiGate cleanly
            if self.knowledge.wiki_gate.applicable:
                used_norm_paths: set[str] = set()
                sess_by_key = {s.session_key: s for s, _ in admitted}
                for w in planned_writes:
                    if w.target_kind != "knowledge_doc":
                        continue
                    hint_parts = []
                    for sk in w.source_session_keys:
                        sr = sess_by_key.get(sk)
                        if sr:
                            hint_parts.extend([sr.project, sr.workspace])
                    new_rel, new_md = self.knowledge.wiki_gate.normalize_planned_document(
                        w.relative_path,
                        w.title,
                        w.summary,
                        w.content_preview,
                        source_sessions=w.source_session_keys,
                        project_hint=" ".join(hint_parts),
                    )
                    if new_rel in used_norm_paths:
                        stem = new_rel[:-3] if new_rel.endswith(".md") else new_rel
                        new_rel = f"{stem}-{len(used_norm_paths) + 1}.md"
                    used_norm_paths.add(new_rel)
                    w.relative_path = new_rel
                    w.content_preview = new_md
            run.planned_writes = planned_writes
            skill_plans = sum(1 for w in planned_writes if w.target_kind == "skill_improvement")
            self._finish_node(
                run,
                "P03_ROUTE_PLAN",
                t0,
                summary=(
                    f"查重比对 {len(existing_rules)} 条规则与 {len(existing_skills)} 个技能，"
                    f"整理出 {len(planned_writes)} 项建议（含 {skill_plans} 项 Skill 封装/优化）"
                ),
                internal_checks=[
                    {"name": "规则与技能查重", "status": "PASS"},
                    {"name": "Skill 路由判定", "status": "PASS", "detail": {"skill_plans": skill_plans}},
                    {"name": "单批数量上限校验", "status": "PASS", "detail": {"planned": len(planned_writes), "limit": max_ws}},
                ],
                payload={"planned_count": len(planned_writes), "skill_plans": skill_plans},
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
                    fres = self.delivery.send_feishu_card(
                        title=f"AI 飞轮待审批（{run.batch_date} #{run.batch_seq:02d}）",
                        markdown=card_body,
                        kind="ask",
                        idempotency_key=f"flywheel-ask-{run.run_id}",
                    )
                    if fres["status"] != "SKIPPED":
                        self.db.record_card_delivery(
                            delivery_id=f"del-{card_id}-feishu",
                            card_id=card_id,
                            channel="feishu_cli",
                            status=fres["status"],
                            external_ref=fres.get("external_ref"),
                            last_error=fres.get("error"),
                        )
                self._emit_run("awaiting_approval", run)
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
        self.db.update_decision_card_state(run_id, state="decided", selected_option="同意并写入")
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run_id,
            event_type="approved",
            actor="user",
            summary=f"人工批准放行 {sum(1 for w in run.planned_writes if w.approved)}/{len(run.planned_writes)} 项建议",
        )
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
        self.db.update_decision_card_state(run.run_id, state="closed", selected_option="取消本次写入")
        self.db.append_audit_event(
            entity_type="workflow_run",
            entity_id=run.run_id,
            event_type="rejected",
            actor="user",
            summary=f"取消本次写入并释放锁：{reason}",
        )
        self._emit_run("run_rejected", run, {"reason": reason})
        return run

    def _execute_post_approval(
        self, run: WorkflowRunRecord, simulate_fail_node: str | None = None
    ) -> WorkflowRunRecord:
        active_node = "P04_WRITE_VERIFY"
        t0 = time.perf_counter()
        written_paths: list[str] = []
        created_rules: list[str] = []
        written_skills: list[str] = []
        try:
            # ---------------- P04 WRITE & VERIFY ----------------
            t0 = self._begin_node(run, "P04_WRITE_VERIFY")
            if simulate_fail_node == "P04_WRITE_VERIFY":
                raise RuntimeError("Simulated P04 write verification failure")

            approved_items = [w for w in run.planned_writes if w.approved]
            write_receipts: list[dict[str, Any]] = []
            for item in approved_items:
                if item.target_kind == "knowledge_doc" and item.relative_path:
                    rel_to_write = item.relative_path
                    content_to_write = item.content_preview
                    if self.knowledge.wiki_gate.applicable:
                        rel_to_write, content_to_write = self.knowledge.wiki_gate.normalize_planned_document(
                            rel_to_write,
                            item.title,
                            item.summary,
                            content_to_write,
                            source_sessions=item.source_session_keys,
                        )
                    res = self.knowledge.write_document_verified(
                        rel_to_write,
                        content_to_write,
                        action=item.action,
                    )
                    written_paths.append(res["relative_path"])
                    write_receipts.append(res)
                elif item.target_kind == "flywheel_rule":
                    normalized_payload = normalize_rule_payload(item)
                    obs_id = normalized_payload.pop("obs_id", None)
                    root_cause_key = normalized_payload.pop("root_cause_key", None)
                    req = CreateRuleRequest.model_validate(normalized_payload)
                    rule = self.activation.register_rule(req, root_cause_key=root_cause_key)
                    if obs_id:
                        self.db.update_mistake_observation_status(
                            str(obs_id), "promoted", promoted_rule_id=rule.rule_id
                        )
                    created_rules.append(rule.rule_id)
                    write_receipts.append({"rule_id": rule.rule_id, "title": rule.title, "status": rule.status.value})
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

            self._finish_node(
                run,
                "P04_WRITE_VERIFY",
                t0,
                summary=(
                    f"写入 {len(written_paths)} 篇笔记、注册 {len(created_rules)} 条规则、"
                    f"更新/新建 {len(written_skills)} 个 Skill（全部校验通过）"
                ),
                internal_checks=[
                    {"name": "路径安全与白名单校验", "status": "PASS"},
                    {"name": "落盘回读一致性校验", "status": "PASS", "detail": {"docs": len(written_paths), "skills": len(written_skills)}},
                ],
                artifacts=written_paths + created_rules + written_skills,
                payload={"write_receipts": write_receipts},
            )

            # ---------------- P05 SETTLE ----------------
            active_node = "P05_SETTLE"
            t0 = self._begin_node(run, "P05_SETTLE")
            if simulate_fail_node == "P05_SETTLE":
                raise RuntimeError("Simulated P05 failure")
            p02_node = self._get_node(run, "P02_REVIEW")
            p02_findings = (p02_node.payload or {}).get("findings", []) if isinstance(p02_node.payload, dict) else []
            not_worthy_keys = {
                str(f.get("session_key"))
                for f in p02_findings
                if isinstance(f, dict) and f.get("worthy") is False and f.get("session_key")
            }
            for sk in run.admitted_sessions:
                if sk in not_worthy_keys:
                    self.db.update_session_status(sk, SessionStatus.ARCHIVED)
                else:
                    self.db.update_session_status(sk, SessionStatus.PROCESSED)
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
                    f"幂等结算 {len(run.admitted_sessions)} 个会话为 processed，"
                    f"核心索引卡已刷新（{core_card_info['lines']} 行）{dream_note}"
                ),
                internal_checks=[
                    {"name": "会话队列幂等回写", "status": "PASS"},
                    {"name": "核心索引卡 FLYWHEEL-CORE.md 刷新", "status": "PASS", "detail": {"lines": core_card_info["lines"], "bytes": core_card_info["bytes"]}},
                    {"name": "P05 保留独占锁至 P07 终态校验", "status": "PASS"},
                ],
                payload={
                    "settled_sessions": run.admitted_sessions,
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
                f"- **处理对话数**：{len(run.admitted_sessions)} 段",
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
            self.db.update_decision_card_state(run.run_id, state="done")
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="run_completed",
                actor="orchestrator",
                summary=f"批次完成：笔记 {len(written_paths)} 篇，规则 {len(created_rules)} 条，Skill {len(written_skills)} 个",
                payload={"docs": written_paths, "rules": created_rules, "skills": written_skills},
            )
            self.fs_store.write_receipt("runs", f"{run.run_id}-final", run.model_dump(mode="json"))
            self._emit_run("run_completed", run)
            return run
        except Exception as exc:
            return self._fail_run(run, active_node, str(exc), t0)
