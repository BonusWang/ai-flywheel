"""Pre-approval pipeline stages: P00 (Prepare), P01 (Begin), P02 (Review), P03 (Route & Plan), and Approval Gate.

P02 and P03 are strictly read-only with respect to knowledge documents, rules, Skills,
mistake observations, and dissent records: candidates and plans stay inside the run record
until approved and executed by P04.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    REVIEW_SOURCE_UNDECLARED,
    PlanUnavailableError,
    ReviewUnavailableError,
)
from ai_flywheel.adapters.wiki import (
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
)
from ai_flywheel.core.pipeline.constants import (
    LANDING_FAILED_REASON,
    PLAN_SOURCE_LABELS,
    REVIEW_SOURCE_LABELS,
)
from ai_flywheel.core.pipeline.helpers import (
    apply_route_decision,
    candidate_dissents,
    candidate_mistakes,
    domain_signal_names,
    eligible_promotions,
    route_knowledge_doc,
    unrouted_reason_breakdown,
)
from ai_flywheel.core.queue_service import (
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_SKIP,
)
from ai_flywheel.models import (
    DistillQueueItem,
    INCIDENT_PAGE_TYPE,
    NodeStatus,
    PlannedWrite,
    QueueStatus,
    RetroStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
    is_incident_write,
)

logger = logging.getLogger(__name__)


@dataclass
class StageTracker:
    """Tracks the currently executing node and its start timestamp for fail-run reporting."""

    active_node: str
    t0: float


def _facade_module() -> Any:
    """The public facade module ``ai_flywheel.core.orchestrator`` (monkeypatch seam)."""
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


class PreApprovalStagesMixin:
    """Cohesive P00–P03 + approval-gate stage implementations mixed into WorkflowOrchestrator."""

    def _run_p00_prepare(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        session_keys: list[str] | None,
        queue_ids: list[str] | None,
        simulate_fail_node: str | None,
    ) -> list[SessionRecord]:
        t0 = self._enter_stage(run, tracker, "P00_PREPARE", simulate_fail_node, "Simulated P00 failure")
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

        return selected_sessions

    def _run_p01_begin(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        selected_sessions: list[SessionRecord],
        session_keys: list[str] | None,
        queue_ids: list[str] | None,
        runner_mode: str,
        simulate_fail_node: str | None,
    ) -> list[tuple[SessionRecord, str]]:
        t0 = self._enter_stage(run, tracker, "P01_BEGIN", simulate_fail_node, "Simulated P01 failure")
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
        taken_out_sessions: list[str] = []
        taken_out_queue_ids: list[str] = []

        def session_taken_out(session_key: str) -> bool:
            """Record a session the client took out of distillation, and say whether it was."""
            if not self._taken_out_of_distillation(session_key):
                return False
            if session_key not in taken_out_sessions:
                taken_out_sessions.append(session_key)
            return True

        if queue_ids:
            # An explicit queue_ids batch jumps the line: claim exactly those items.
            # Naming an item is not the explicit enqueue that takes a session back:
            # one of a session taken out of distillation is left where it is.
            for qid in dict.fromkeys(str(q) for q in queue_ids if str(q).strip()):
                item = self.db.get_queue_item(qid)
                if item is None:
                    continue
                sess = self.db.get_session(item.session_key)
                if sess is None:
                    continue
                if session_taken_out(item.session_key):
                    if item.status is QueueStatus.QUEUED:
                        # Left in the queue by a run that ended after the declaration:
                        # it gets the end state the declaration gives it, not another claim.
                        self.queue.on_hand_distilled_skip(item.session_key, actor="p01_begin")
                        taken_out_queue_ids.append(item.queue_id)
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
            # item of an admitted session is claimed along with it. A session taken
            # out of distillation is not admitted, named or not: it produces nothing.
            for sess in selected_sessions[:max_sess]:
                if session_taken_out(sess.session_key):
                    continue
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
                if session_taken_out(item.session_key):
                    # Only a run that ended after the declaration leaves a queued item
                    # of such a session behind. It is excluded here as the declaration
                    # would have excluded it, instead of being claimed or left to wait.
                    self.queue.on_hand_distilled_skip(item.session_key, actor="p01_begin")
                    taken_out_queue_ids.append(item.queue_id)
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
                    if session_taken_out(sess.session_key):
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
        taken_out_note = (
            f"，{len(taken_out_sessions)} 个会话已声明 hand_distilled skip（完全跳过），未准入"
            if taken_out_sessions
            else ""
        )
        if taken_out_sessions:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="hand_distilled_skip_not_admitted",
                actor="p01_begin",
                summary=(
                    f"{len(taken_out_sessions)} 个会话已声明 hand_distilled skip（完全跳过），"
                    "未准入，本批不产出其任何内容"
                ),
                payload={
                    "session_keys": list(taken_out_sessions),
                    "excluded_queue_ids": list(taken_out_queue_ids),
                },
            )
        self._finish_node(
            run,
            "P01_BEGIN",
            t0,
            summary=(
                f"独占锁已获取，准入 {len(admitted)} 个会话单元（共 {used_chars} 字符）"
                f"{queue_note}{governance_note}{taken_out_note}"
            ),
            internal_checks=[
                {"name": "独占锁持有核验", "status": "PASS"},
                {"name": "预算与字符上限准入", "status": "PASS", "detail": {"chars": used_chars, "limit": max_chars}},
                {"name": "队列项认领与 run 绑定", "status": "PASS", "detail": {"claimed": len(claimed_queue_ids)}},
                *(
                    [
                        {
                            "name": "已声明完全跳过会话的准入排除",
                            "status": "PASS",
                            "detail": {
                                "session_keys": list(taken_out_sessions),
                                "excluded_queue_ids": len(taken_out_queue_ids),
                            },
                        }
                    ]
                    if taken_out_sessions
                    else []
                ),
            ],
            payload={
                "admitted_sessions": run.admitted_sessions,
                "used_chars": used_chars,
                "claimed_queue_ids": claimed_queue_ids,
                # How each admitted session is distilled: a `hand_distilled` session is
                # reviewed in full, but P03 writes no knowledge document for it.
                "distill_modes": distill_modes,
                "governance_only_session_keys": governance_only_keys,
                # Sessions named or due for this batch that were taken out of distillation, and so not admitted.
                "hand_distilled_skipped_session_keys": list(taken_out_sessions),
                "hand_distilled_excluded_queue_ids": list(taken_out_queue_ids),
            },
        )
        self._publish_queue_update("p01_claim", run.run_id, claimed_queue_ids + taken_out_queue_ids)

        return admitted

    def _run_p02_review(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        admitted: list[tuple[SessionRecord, str]],
        resolved_phase: str,
        simulate_fail_node: str | None,
    ) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
        t0 = self._enter_stage(run, tracker, "P02_REVIEW", simulate_fail_node, "Simulated P02 failure")
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

        return runner, existing_skills, review_findings

    def _run_p03_route_plan(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        admitted: list[tuple[SessionRecord, str]],
        runner: Any,
        existing_skills: list[dict[str, Any]],
        review_findings: dict[str, Any],
        resolved_phase: str,
        simulate_fail_node: str | None,
    ) -> list[PlannedWrite]:
        t0 = self._enter_stage(run, tracker, "P03_ROUTE_PLAN", simulate_fail_node, "Simulated P03 failure")
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
        # A session taken out of distillation entirely (`governance_only=false`) is one
        # step further: it is not admitted, and whatever the plan still puts behind it —
        # a document, a rule, a Skill plan — is taken out too.
        # The declaration is read again rather than taken from P01/P02: it may have been
        # made while this batch was already running. This never fails the node: a plan
        # left with nothing to write is an empty plan, not an unavailable one.
        governance_only_keys = sorted(
            {
                *self._declared_keys(
                    run.admitted_sessions,
                    frozenset({DISTILL_MODE_GOVERNANCE_ONLY, DISTILL_MODE_SKIPPED}),
                ),
                *(review_findings.get("governance_only_session_keys") or []),
            }
        )
        hand_plan, _taken_out_sessions = self._leave_out_hand_distilled(
            run,
            planned_writes,
            actor="p03_plan",
            documents_summary="篇笔记所属会话已由客户端手工沉淀，仅做治理提炼，未列入写入计划",
            items_summary="项计划所属会话已被声明 hand_distilled skip（完全跳过），或本批未准入任何会话，未列入写入计划",
        )
        planned_writes = hand_plan.kept
        governance_only_skipped = hand_plan.documents_left_out
        taken_out_skipped = hand_plan.items_left_out
        if not self.knowledge.wiki_gate.applicable:
            plan_info.kept_count = len(planned_writes)
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
        taken_out_note = (
            f"；{len(taken_out_skipped)} 项计划所属会话已被声明 hand_distilled skip（完全跳过）"
            f"或本批未准入任何会话，未列入计划"
            if taken_out_skipped
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
                f"{dropped_note}{unrouted_note}{capacity_note}{domain_note}{incident_note}{deferred_note}{governance_note}{taken_out_note}"
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
                            "name": "无送审会话或已声明完全跳过会话的计划项排除",
                            "status": "PASS",
                            "detail": {
                                "taken_out_session_keys": sorted(_taken_out_sessions),
                                "items": len(taken_out_skipped),
                            },
                        }
                    ]
                    if taken_out_skipped
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
                # Plan items of every kind left out: their sessions were taken out of
                # distillation entirely (hand_distilled skip), or the batch admitted nothing.
                "taken_out_session_keys": sorted(_taken_out_sessions),
                "items_left_out": taken_out_skipped,
                "plan_item_unknown_source_keys": hand_plan.unknown_source_keys,
            },
        )

        return planned_writes

    def _handle_approval_gate(
        self,
        run: WorkflowRunRecord,
        planned_writes: list[PlannedWrite],
        init_approval_mode: str,
    ) -> bool:
        """Return ``True`` if the run paused awaiting manual approval, ``False`` to proceed to P04."""
        # Check Approval Gate (supports smart | always | auto). The mode is the one this
        # batch was started under — valve and all — not a second reading of the request.
        effective_mode = init_approval_mode

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
            return True

        if planned_writes:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="auto_approved",
                actor="smart_gate",
                summary=f"智能审批自动放行 {len(planned_writes)} 项常规变更",
                payload={"approval_mode": effective_mode, "planned_count": len(planned_writes)},
            )

        return False
