from __future__ import annotations

import subprocess
from typing import Any

import httpx

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import WorkflowRunRecord


class DeliveryAdapter:
    """P07 Delivery adapters: local portal digest + optional exact write-set Git closure & Webhook."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg

    def deliver(self, run: WorkflowRunRecord, written_paths: list[str]) -> dict[str, Any]:
        results: dict[str, Any] = {
            "local_portal": "PASS" if self.cfg.delivery.local_portal else "SKIPPED",
            "git_closure": "SKIPPED",
            "webhook": "SKIPPED",
        }

        if (
            self.cfg.delivery.git_closure.enabled
            and self.cfg.safety_valves.git_closure_enabled
            and written_paths
        ):
            results["git_closure"] = self._git_closure(written_paths, run.run_id)

        if self.cfg.delivery.webhook.enabled and self.cfg.delivery.webhook.url:
            results["webhook"] = self._post_webhook(run)

        return results

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
