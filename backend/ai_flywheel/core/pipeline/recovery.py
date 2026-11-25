"""Lock checkpoints, heartbeat, interrupted-run recovery and the fail-run contract.

Every method here works on the orchestrator instance it is mixed into: it reads
``self.db`` / ``self.fs_store`` / ``self.cfg`` / ``self.delivery`` and raises the
run's state through ``self._get_node`` and ``self._emit_run``. Keeping the paths
that close a run in one module is what makes the three terminal routes — the
fail-run contract, the reaper and a cancellation — share one write-back sequence.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from ai_flywheel.models import (
    NodeStatus,
    RunStatus,
    SessionStatus,
    WorkflowRunRecord,
    utc_now_iso,
)
from ai_flywheel.storage.db import RunAlreadyFinalizedError

from ai_flywheel.core.pipeline.constants import LockLostError


class RecoveryMixin:
    """Heartbeat, lock checkpoints and the terminal paths of a run."""

    # ---------------- heartbeat & lock checkpoint ----------------

    def _start_heartbeat(self, run_id: str) -> None:
        self.heartbeat.start(run_id)

    def _stop_heartbeat(self) -> None:
        self.heartbeat.stop()

    def _ensure_lock(self, run: WorkflowRunRecord) -> None:
        """Synchronous lock checkpoint before a side effect: renew or fail the run."""
        if not self.db.renew_workflow_lock(run.run_id):
            raise LockLostError(f"执行期间失去独占锁：{run.run_id}（锁缺失或已易主）")

    # ---------------- reaping runs whose executor is gone ----------------

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

    # ---------------- terminal bookkeeping ----------------

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
        """Apply the fail-run contract: FAILED node, SKIPPED downstream, released lock."""
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
        self._mark_remaining_nodes_skipped(run, reason=f"因前置节点 {failed_node_id} 失败而跳过")

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
            if not applied:
                return
            if self.cfg.delivery.webhook.enabled:
                self.delivery.notify_failure(
                    run=run,
                    failed_node=failed_node_id,
                    reason=reason,
                )
            if self.cfg.delivery.feishu_cli.enabled:
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
