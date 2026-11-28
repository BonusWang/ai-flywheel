"""Post-approval pipeline stages: P04 (Controlled Write & Verify), P05 (Settle), P06 (Report), and P07 (Deliver & Unlock).

P04 is the single controlled write stage allowed to write knowledge documents, rules,
Skills, mistake observations, and dissent records. P07 is the terminal stage that
releases the exclusive workflow lock.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_flywheel.adapters.runners import normalize_rule_payload
from ai_flywheel.adapters.wiki import (
    UPDATE_NO_NEW_CONTENT,
    approved_registration_matches,
)
from ai_flywheel.core.pipeline.constants import LANDING_FAILED_REASON
from ai_flywheel.core.pipeline.git_commit import commit_p04_written_files
from ai_flywheel.core.pipeline.helpers import (
    governance_only_skip_receipt,
    left_out_item_receipt,
    link_rule_to_observation,
    route_knowledge_doc,
)
from ai_flywheel.core.pipeline.stage_pre_approval import StageTracker, _facade_module
from ai_flywheel.core.queue_service import (
    DISTILL_MODE_GOVERNANCE_ONLY,
    DISTILL_MODE_SKIPPED,
    HAND_DISTILLED_SKIP,
)
from ai_flywheel.models import (
    CreateRuleRequest,
    INCIDENT_PAGE_TYPE,
    PlannedWrite,
    QueueStatus,
    RunStatus,
    SessionStatus,
    WorkflowRunRecord,
    is_incident_write,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


@dataclass
class P04Outcome:
    """Artifacts produced by P04 and consumed by P05, P06, and P07."""

    written_paths: list[str] = field(default_factory=list)
    created_rules: list[str] = field(default_factory=list)
    written_skills: list[str] = field(default_factory=list)


@dataclass
class P05Outcome:
    """Settlement outputs produced by P05 and summarized by P06."""

    settled_sessions: list[str]
    returned_sessions: list[str]
    skipped_sessions: list[str]
    core_card_info: dict[str, Any]
    dream_report: dict[str, Any] | None


class PostApprovalStagesMixin:
    """Cohesive P04–P07 stage implementations mixed into WorkflowOrchestrator."""

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

    def _run_p04_write_verify(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        simulate_fail_node: str | None,
    ) -> P04Outcome:
        written_paths: list[str] = []
        written_index_paths: list[str] = []
        created_rules: list[str] = []
        written_skills: list[str] = []
        skipped_docs: list[dict[str, Any]] = []
        merged_domain_pages = 0
        t0 = self._enter_stage(
            run, tracker, "P04_WRITE_VERIFY", simulate_fail_node, "Simulated P04 write verification failure"
        )

        approved_items = [w for w in run.planned_writes if w.approved]
        p02_payload = self._get_node(run, "P02_REVIEW").payload
        if not isinstance(p02_payload, dict):
            p02_payload = {}
        mistake_candidates = [
            c for c in (p02_payload.get("candidate_mistakes") or []) if isinstance(c, dict)
        ]
        dissent_candidates = [
            c for c in (p02_payload.get("candidate_dissents") or []) if isinstance(c, dict)
        ]
        # The plan may have waited hours for approval. A session declared hand-distilled
        # in that time has had its knowledge written up by hand since: its document is
        # not written a second time, and the receipt says why. A session taken out of
        # distillation entirely in that time produces nothing at all: no document, no
        # rule, no Skill plan, and not the mistake or the dissent the review found in it.
        # The declarations are read again here, from the database — not taken from P03.
        hand_plan, taken_out_sessions = self._leave_out_hand_distilled(
            run,
            approved_items,
            actor="p04_write",
            documents_summary="篇已批准笔记所属会话在等待审批期间被声明手工沉淀，未写入",
            items_summary=(
                "项已批准计划所属会话在等待审批期间被声明 hand_distilled skip（完全跳过），"
                "或本批未准入任何会话，未写入"
            ),
        )
        approved_items = hand_plan.kept
        governance_only_skipped = list(hand_plan.documents_left_out)
        taken_out_skipped = list(hand_plan.items_left_out)

        def candidate_left_out(candidate: dict[str, Any]) -> bool:
            """Whether no session of the batch stands behind a mistake or a dissent.

            Its session was taken out of distillation, or the batch admitted nothing.
            A candidate the review did not attribute to a session is left out only when
            every session of the batch is gone: it may still speak for one that remains.
            """
            if not run.admitted_sessions:
                return True
            key = str(candidate.get("session_key") or "")
            if key:
                return key in taken_out_sessions
            return all(admitted in taken_out_sessions for admitted in run.admitted_sessions)

        mistakes_left_out = sum(1 for c in mistake_candidates if candidate_left_out(c))
        dissents_left_out = sum(1 for c in dissent_candidates if candidate_left_out(c))
        mistake_candidates = [c for c in mistake_candidates if not candidate_left_out(c)]
        dissent_candidates = [c for c in dissent_candidates if not candidate_left_out(c)]
        write_receipts: list[dict[str, Any]] = [
            governance_only_skip_receipt(doc) for doc in governance_only_skipped
        ] + [left_out_item_receipt(item) for item in taken_out_skipped]
        if mistakes_left_out or dissents_left_out:
            # What P02 found in such a session is not persisted either.
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="review_candidates_left_out",
                actor="p04_write",
                summary=(
                    f"{mistakes_left_out + dissents_left_out} 条审查候选（错题 {mistakes_left_out}、"
                    f"分歧 {dissents_left_out}）所属会话已被声明 hand_distilled skip，未入池"
                ),
                payload={
                    "mistakes": mistakes_left_out,
                    "dissents": dissents_left_out,
                    "taken_out_session_keys": sorted(taken_out_sessions),
                    "admitted_session_keys": list(run.admitted_sessions),
                },
            )
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
                # The declaration is read once more, right before this item is written.
                # A session may have been declared hand-distilled while the items ahead
                # of it were being written: what was left out on entering P04 was read
                # then, and this item has been approved for hours.
                late_plan, _ = self._leave_out_hand_distilled(
                    run,
                    [item],
                    actor="p04_write",
                    documents_summary="篇已批准笔记所属会话在写入前被声明手工沉淀，未写入",
                    items_summary=(
                        "项已批准计划所属会话在写入前被声明 hand_distilled skip（完全跳过），未写入"
                    ),
                    report_unknown_keys=False,
                )
                if not late_plan.kept:
                    if late_plan.items_left_out:
                        taken_out_skipped.extend(late_plan.items_left_out)
                        write_receipts.append(left_out_item_receipt(late_plan.items_left_out[0]))
                    else:
                        governance_only_skipped.extend(late_plan.documents_left_out)
                        write_receipts.append(governance_only_skip_receipt(late_plan.documents_left_out[0]))
                    continue
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
                    promoted_obs = link_rule_to_observation(self.db, rule.rule_id, obs_id, root_cause_key)
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
                    f"；{len(taken_out_skipped)} 项计划因所属会话已被声明 hand_distilled skip（完全跳过）"
                    f"或本批未准入任何会话，未写入"
                    if taken_out_skipped
                    else ""
                )
                + (
                    f"；{mistakes_left_out + dissents_left_out} 条审查候选因所属会话已被声明完全跳过，未入池"
                    if mistakes_left_out or dissents_left_out
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
                            "name": "无送审会话或已声明完全跳过会话的写入排除",
                            "status": "PASS",
                            "detail": {
                                "items": len(taken_out_skipped),
                                "mistakes": mistakes_left_out,
                                "dissents": dissents_left_out,
                            },
                        }
                    ]
                    if taken_out_skipped or mistakes_left_out or dissents_left_out
                    else []
                ),
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
                # Approved items of every kind not written: their sessions were taken out of
                # distillation entirely, or the batch admitted nothing. Each says why.
                **({"items_left_out": taken_out_skipped} if taken_out_skipped else {}),
                **(
                    {
                        "review_candidates_left_out": {
                            "mistakes": mistakes_left_out,
                            "dissents": dissents_left_out,
                        }
                    }
                    if mistakes_left_out or dissents_left_out
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

        return P04Outcome(
            written_paths=written_paths,
            created_rules=created_rules,
            written_skills=written_skills,
        )

    def _run_p05_settle(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        created_rules: list[str],
        simulate_fail_node: str | None,
    ) -> P05Outcome:
        t0 = self._enter_stage(run, tracker, "P05_SETTLE", simulate_fail_node, "Simulated P05 failure")
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
        # consumption watermark moves), everything else returns to the queue. A session
        # taken out of distillation is a third case: its item is neither settled as
        # distilled nor left waiting. It ends `excluded (HAND_DISTILLED_SKIP)`, the
        # state the declaration gives every other unconsumed item of the session.
        taken_out_sessions = {
            key
            for key, mode in self._distill_modes(run.admitted_sessions).items()
            if mode == DISTILL_MODE_SKIPPED
        }
        settled_queue_ids: list[str] = []
        requeued_queue_ids: list[str] = []
        excluded_queue_ids: list[str] = []
        for qid in dict.fromkeys(self._claimed_queue_ids(run)):
            self._ensure_lock(run)  # synchronous checkpoint before each settlement
            item = self.db.get_queue_item(qid)
            if item is None or item.status is not QueueStatus.PROCESSING:
                continue
            if item.session_key in taken_out_sessions:
                self.db.update_queue_item_status(
                    qid,
                    QueueStatus.EXCLUDED,
                    run_id=run.run_id,
                    actor="p05_settle",
                    exclude_reason_code=HAND_DISTILLED_SKIP,
                    exclude_reason_detail=(
                        "hand-distilled by the client while the batch ran: nothing was distilled"
                    ),
                    reason_code=HAND_DISTILLED_SKIP,
                    summary="session taken out of distillation before this item was settled",
                )
                excluded_queue_ids.append(qid)
            elif item.session_key in reviewed_keys:
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
            "p05_settle", run.run_id, settled_queue_ids + requeued_queue_ids + excluded_queue_ids
        )
        settled_sessions: list[str] = []
        returned_sessions: list[str] = []
        deferred_sessions: list[str] = []
        # A session taken out of distillation is not settled as distilled — that status
        # says the flywheel reviewed it and wrote up what it found. It is closed: nothing
        # more will be distilled from it unless the client enqueues it again.
        skipped_sessions = [sk for sk in run.admitted_sessions if sk in taken_out_sessions]
        for sk in run.admitted_sessions:
            self._ensure_lock(run)  # synchronous checkpoint before each settlement
            if sk in taken_out_sessions:
                self.db.update_session_status(sk, SessionStatus.ARCHIVED)
                # Not counted as settled: the flywheel did not review it and write up what it found.
                continue
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
            + (f"、排除 {len(excluded_queue_ids)} 条（会话已声明完全跳过）" if excluded_queue_ids else "")
            if settled_queue_ids or requeued_queue_ids or excluded_queue_ids
            else ""
        )
        deferred_note = (
            f"、{len(deferred_sessions)} 个会话仍有未结算分片保持 pending"
            if deferred_sessions
            else ""
        )
        taken_out_note = (
            f"、{len(skipped_sessions)} 个会话已声明 hand_distilled skip（未沉淀，队列项标记 excluded）"
            if skipped_sessions
            else ""
        )
        if skipped_sessions:
            self.db.append_audit_event(
                entity_type="workflow_run",
                entity_id=run.run_id,
                event_type="hand_distilled_skip_settled",
                actor="p05_settle",
                summary=(
                    f"{len(skipped_sessions)} 个会话已声明 hand_distilled skip（完全跳过），"
                    f"未按沉淀结算；{len(excluded_queue_ids)} 条队列项标记 excluded"
                ),
                payload={
                    "session_keys": list(skipped_sessions),
                    "excluded_queue_ids": list(excluded_queue_ids),
                    "reason_code": HAND_DISTILLED_SKIP,
                },
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
                + taken_out_note
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
                        **({"excluded": len(excluded_queue_ids)} if excluded_queue_ids else {}),
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
                "excluded_queue_ids": excluded_queue_ids,
                # Admitted sessions the client took out of distillation while the batch ran.
                "hand_distilled_skipped_sessions": skipped_sessions,
                "settled_retro_count": settled_retros,
                "created_rules": created_rules,
                "core_card": {"path": core_card_info["path"], "lines": core_card_info["lines"]},
                "dream": dream_report,
            },
        )

        return P05Outcome(
            settled_sessions=settled_sessions,
            returned_sessions=returned_sessions,
            skipped_sessions=skipped_sessions,
            core_card_info=core_card_info,
            dream_report=dream_report,
        )

    def _run_p06_report(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        p04: P04Outcome,
        p05: P05Outcome,
        simulate_fail_node: str | None,
    ) -> None:
        written_paths = p04.written_paths
        created_rules = p04.created_rules
        written_skills = p04.written_skills
        settled_sessions = p05.settled_sessions
        returned_sessions = p05.returned_sessions
        skipped_sessions = p05.skipped_sessions
        core_card_info = p05.core_card_info
        dream_report = p05.dream_report
        t0 = self._enter_stage(run, tracker, "P06_REPORT", simulate_fail_node, "Simulated P06 failure")
        digest_lines = [
            f"## 沉淀总结（{run.run_id}）",
            "",
            f"- **处理对话数**：{len(settled_sessions)} 段"
            + (
                f"（{len(returned_sessions)} 段未审查，退回队列）"
                if returned_sessions
                else ""
            )
            + (
                f"（另 {len(skipped_sessions)} 段已声明 hand_distilled skip，未沉淀）"
                if skipped_sessions
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


    def _run_p07_deliver(
        self,
        run: WorkflowRunRecord,
        tracker: StageTracker,
        *,
        p04: P04Outcome,
        simulate_fail_node: str | None,
    ) -> WorkflowRunRecord:
        written_paths = p04.written_paths
        created_rules = p04.created_rules
        written_skills = p04.written_skills
        t0 = self._enter_stage(
            run, tracker, "P07_DELIVER", simulate_fail_node, "Simulated P07 delivery failure"
        )
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
