from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.channels.webhook import WebhookChannel
from ai_flywheel.adapters.lark_cli import LarkCliRunner
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import PlannedWrite, WorkflowRunRecord


class DeliveryAdapter:
    """P07 Delivery & notification adapters: local portal, optional Git closure, Webhook, and Feishu CLI.

    Feishu notifications (alert / optional fyi) go through the shared LarkCliRunner so
    they never pass through a cmd.exe shim; status distinguishes sent / failed /
    unknown (timeout or killed process — the message may still have been delivered).
    Webhook notifications go through the generic channel, which sanitizes every
    outbound string and never raises on a transport error.
    """

    def __init__(
        self,
        cfg: FlywheelConfig,
        lark_runner: LarkCliRunner | None = None,
        webhook_channel: WebhookChannel | None = None,
    ) -> None:
        self.cfg = cfg
        self._lark = lark_runner
        self._webhook = webhook_channel

    def set_lark_runner(self, runner: LarkCliRunner | None) -> None:
        """Share one resolved lark-cli invocation with the rest of the process.

        The Feishu channel and the delivery adapter would otherwise freeze two
        copies of the same executable — and only one of them would carry the
        configured identity.
        """
        self._lark = runner

    @property
    def lark(self) -> LarkCliRunner:
        if self._lark is None:
            fcfg = self.cfg.delivery.feishu_cli
            self._lark = LarkCliRunner(fcfg.command, fcfg.command_args, identity=fcfg.identity)
        return self._lark

    def _webhook_channel(self) -> WebhookChannel:
        """The webhook bound to the *current* configuration.

        A saved settings patch replaces ``cfg.delivery`` wholesale, so the
        channel is built per notification unless a test injected one.
        """
        if self._webhook is not None:
            return self._webhook
        return WebhookChannel(self.cfg.delivery.webhook)

    # ---------------- Webhook notifications ----------------

    def notify_approval_needed(
        self,
        *,
        run: WorkflowRunRecord,
        reasons: list[str],
        planned_writes: list[PlannedWrite | dict[str, Any]],
        portal_url: str,
        title: str = "",
    ) -> dict[str, Any]:
        """Tell the webhook that a batch is waiting for a human decision."""
        return self._webhook_channel().notify(
            event_type="awaiting_approval",
            run=run,
            title=title or f"AI 飞轮待审批（{run.batch_date or run.run_id}）",
            summary=f"批次 {run.batch_date or run.run_id} 等待人工确认，共 {len(planned_writes)} 项写入计划。",
            portal_url=portal_url,
            reasons=list(reasons),
            planned_writes=list(planned_writes),
        )

    def notify_run_completed(self, run: WorkflowRunRecord) -> dict[str, Any]:
        """Tell the webhook that a batch reached its terminal PASS state."""
        return self._webhook_channel().notify(
            event_type="run_completed",
            run=run,
            title=f"AI 飞轮批次完成（{run.batch_date or run.run_id}）",
            summary=run.digest_markdown or f"批次 {run.run_id} 已完成。",
        )

    def notify_failure(
        self,
        *,
        run: WorkflowRunRecord,
        failed_node: str,
        reason: str,
    ) -> dict[str, Any]:
        """Tell the webhook that a batch failed at one node."""
        return self._webhook_channel().notify(
            event_type="run_failed",
            run=run,
            title=f"AI 飞轮运行异常（{run.batch_date or run.run_id}）",
            summary=f"失败环节：{failed_node}；原因：{reason}",
            reasons=[reason],
        )

    def notify_fyi(self, *, title: str, summary: str, run: WorkflowRunRecord | None = None) -> dict[str, Any]:
        """A routine notice. Sent only when ``delivery.webhook.notify_on_fyi`` is on."""
        return self._webhook_channel().notify(
            event_type="fyi",
            run=run,
            title=title,
            summary=summary,
        )

    def deliver(self, run: WorkflowRunRecord, written_paths: list[str]) -> dict[str, Any]:
        results: dict[str, Any] = {
            "local_portal": "PASS" if self.cfg.delivery.local_portal else "SKIPPED",
            "git_closure": "SKIPPED",
            "webhook": "SKIPPED",
            "feishu_cli": "SKIPPED",
        }

        if (
            self.cfg.delivery.git_closure.enabled
            and self.cfg.safety_valves.git_closure_enabled
            and written_paths
        ):
            results["git_closure"] = self._git_closure(written_paths, run.run_id)

        if self.cfg.delivery.webhook.enabled and self.cfg.delivery.webhook.url:
            webhook_res = self.notify_run_completed(run)
            results["webhook"] = (
                f"PASS ({webhook_res['status_code']})"
                if webhook_res["sent"]
                else f"FAILED ({webhook_res['reason']})"
            )

        if self.cfg.delivery.feishu_cli.enabled and self.cfg.delivery.feishu_cli.notify_on_fyi:
            fres = self.send_feishu_text_notice(
                title=f"每日沉淀已完成（{run.batch_date or run.run_id}）",
                markdown=run.digest_markdown or f"批次 `{run.run_id}` 已自动完成。",
                kind="fyi",
                idempotency_key=f"flywheel-fyi-{run.run_id}",
            )
            results["feishu_cli"] = fres["status"]

        return results

    def send_feishu_text_notice(
        self,
        *,
        title: str,
        markdown: str,
        kind: str = "ask",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Send an important decision ('ask'), alert, or optional 'fyi' message via local lark-cli.

        The name says what it does: it sends a plain-text notice. Interactive
        cards go through the Feishu channel, not through here.
        """
        fcfg = self.cfg.delivery.feishu_cli
        if not fcfg.enabled:
            return {"status": "SKIPPED", "reason": "feishu_cli disabled", "external_ref": None, "error": None}
        if kind == "fyi" and not fcfg.notify_on_fyi:
            return {"status": "SKIPPED", "reason": "notify_on_fyi disabled", "external_ref": None, "error": None}
        if not fcfg.user_id and not fcfg.chat_id:
            return {"status": "SKIPPED", "reason": "no user_id or chat_id configured", "external_ref": None, "error": None}

        plain_text = f"【{title}】\n\n{markdown}".replace("**", "")[:3500]
        res = self.lark.send_text(
            user_id=fcfg.user_id,
            chat_id=fcfg.chat_id if not fcfg.user_id else "",
            text=plain_text,
            idempotency_key=idempotency_key,
        )
        return {"status": res.status, "reason": None, "external_ref": res.message_id, "error": res.error}

    def send_feishu_card(self, **kwargs: Any) -> dict[str, Any]:
        """Backward-compatible alias for :meth:`send_feishu_text_notice`.

        The old name promised a card and delivered plain text. It stays as an
        alias so existing callers keep working; new code should say what it
        actually sends.
        """
        return self.send_feishu_text_notice(**kwargs)

    def _git_closure(self, written_paths: list[str], run_id: str) -> str:
        """Commit the exact write-set in the knowledge repo, then publish it.

        The remote and the branch come from ``delivery.git_closure`` (they
        default to ``origin`` / ``main``). Publishing is skipped — never fatal —
        when the clone has no such remote or the checked-out branch is not the
        configured one: the commit is the deliverable, and a misconfigured
        remote must not fail a batch whose writes already landed.
        """
        kb_root = self.cfg.knowledge_root_path
        if not (kb_root / ".git").exists():
            return "SKIPPED (not a git repo)"
        add_proc = subprocess.run(
            ["git", "add", "--", *written_paths],
            cwd=str(kb_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if add_proc.returncode != 0:
            raise RuntimeError(f"git add failed: {add_proc.stderr}")
        commit_proc = subprocess.run(
            ["git", "commit", "-m", f"flywheel: sediment run {run_id}"],
            cwd=str(kb_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_proc.returncode != 0 and "nothing to commit" not in commit_proc.stdout.lower():
            raise RuntimeError(f"git commit failed: {commit_proc.stderr}")
        return f"PASS (committed exact write-set{self._publish_commit(kb_root)})"

    def _publish_commit(self, kb_root: Path) -> str:
        """Push the committed write-set to the configured remote/branch.

        Returns a short suffix describing what happened, or an empty string
        when there is nothing to publish.
        """
        closure = self.cfg.delivery.git_closure
        remote = (closure.remote or "").strip()
        branch = (closure.branch or "").strip()
        if not remote or not branch:
            return ""

        remotes = subprocess.run(
            ["git", "remote"],
            cwd=str(kb_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if remote not in remotes.stdout.split():
            return f"; push skipped (no remote '{remote}' in this clone)"

        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(kb_root),
            capture_output=True,
            text=True,
            check=False,
        )
        current_branch = current.stdout.strip()
        if current_branch != branch:
            return f"; push skipped (on branch '{current_branch}', configured '{branch}')"

        push = subprocess.run(
            ["git", "push", remote, branch],
            cwd=str(kb_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if push.returncode != 0:
            raise RuntimeError(f"git push {remote} {branch} failed: {push.stderr or push.stdout}")
        return f"; pushed to {remote}/{branch}"
