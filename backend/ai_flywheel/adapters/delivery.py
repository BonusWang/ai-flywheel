from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import httpx

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import WorkflowRunRecord


class DeliveryAdapter:
    """P07 Delivery & notification adapters: local portal, optional Git closure, Webhook, and Feishu CLI."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg

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
            results["webhook"] = self._post_webhook(run)

        if self.cfg.delivery.feishu_cli.enabled and self.cfg.delivery.feishu_cli.notify_on_fyi:
            fres = self.send_feishu_card(
                title=f"每日沉淀已完成（{run.batch_date or run.run_id}）",
                markdown=run.digest_markdown or f"批次 `{run.run_id}` 已自动完成。",
                kind="fyi",
                idempotency_key=f"flywheel-fyi-{run.run_id}",
            )
            results["feishu_cli"] = fres["status"]

        return results

    def send_feishu_card(
        self,
        *,
        title: str,
        markdown: str,
        kind: str = "ask",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Send an important decision ('ask'), alert, or optional 'fyi' message via local lark-cli."""
        fcfg = self.cfg.delivery.feishu_cli
        if not fcfg.enabled:
            return {"status": "SKIPPED", "reason": "feishu_cli disabled", "external_ref": None, "error": None}
        if kind == "fyi" and not fcfg.notify_on_fyi:
            return {"status": "SKIPPED", "reason": "notify_on_fyi disabled", "external_ref": None, "error": None}
        if not fcfg.user_id and not fcfg.chat_id:
            return {"status": "SKIPPED", "reason": "no user_id or chat_id configured", "external_ref": None, "error": None}

        exe = shutil.which(fcfg.command) or fcfg.command
        # Use single-line JSON via --content so Windows .cmd wrappers never split on raw newlines
        plain_text = f"【{title}】\n\n{markdown}".replace("**", "")
        content_json = json.dumps({"text": plain_text[:3500]}, ensure_ascii=False)
        cmd = [
            exe,
            "im",
            "+messages-send",
            "--as",
            fcfg.identity or "bot",
        ]
        if fcfg.user_id:
            cmd.extend(["--user-id", fcfg.user_id])
        else:
            cmd.extend(["--chat-id", fcfg.chat_id])
        cmd.extend(["--msg-type", "text", "--content", content_json, "--format", "json"])
        if idempotency_key:
            cmd.extend(["--idempotency-key", idempotency_key[:64]])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]
                return {"status": "failed", "external_ref": None, "error": err}
            ext_ref: str | None = None
            try:
                parsed = json.loads(proc.stdout)
                ext_ref = (
                    parsed.get("data", {}).get("message_id")
                    or parsed.get("message_id")
                )
            except Exception:
                pass
            return {"status": "sent", "external_ref": ext_ref, "error": None}
        except Exception as exc:
            return {"status": "failed", "external_ref": None, "error": str(exc)[:300]}

    def _git_closure(self, written_paths: list[str], run_id: str) -> str:
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
        return "PASS (committed exact write-set)"

    def _post_webhook(self, run: WorkflowRunRecord) -> str:
        resp = httpx.post(
            self.cfg.delivery.webhook.url,
            json={
                "run_id": run.run_id,
                "status": run.status.value,
                "phase": run.phase,
                "digest": run.digest_markdown,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return f"PASS ({resp.status_code})"
