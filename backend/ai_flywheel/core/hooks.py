from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ai_flywheel.models import (
    PreflightRequest,
    RegisterSessionRequest,
    ReportSignalRequest,
    SignalKind,
)
from ai_flywheel.storage.fs_store import canonical_hash

if TYPE_CHECKING:
    from ai_flywheel.runtime import FlywheelRuntime


READONLY_TOOLS = {
    "read_file",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "view_image",
    "list_dir",
    "apply_patch",
}

READONLY_CMD_PREFIXES = (
    "get-content",
    "select-string",
    "rg ",
    "git status",
    "git log",
    "git diff",
    "ls",
    "dir",
    "cat ",
    "head ",
    "tail ",
)


class ThinHookProcessor:
    """Stateless fail-open hook receiver for Codex and Claude Code thin forwarders.

    All methodology, retry-breaker, and registration logic stays inside ai-flywheel;
    client hooks remain 5-line forwarders with zero business rules.
    """

    def __init__(self, runtime: FlywheelRuntime) -> None:
        self.runtime = runtime

    def handle(self, client: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = str(
                payload.get("hook_event_name")
                or payload.get("event")
                or payload.get("type")
                or ""
            )
            session_id = str(payload.get("session_id") or payload.get("thread_id") or "")
            turn_id = str(payload.get("turn_id") or "")

            if event == "UserPromptSubmit":
                return self._handle_prompt(client, session_id, turn_id, payload)
            if event == "PreToolUse":
                return self._handle_pre_tool(client, session_id, turn_id, payload)
            if event in {"Stop", "SessionEnd", "SessionStart"}:
                return self._handle_session_lifecycle(client, event, session_id, payload)
            return {"continue": True}
        except Exception as exc:
            # Always fail-open: never block the user's AI client
            return {
                "continue": True,
                "flywheel_warning": f"hook fail-open: {exc}",
            }

    def _handle_prompt(
        self, client: str, session_id: str, turn_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or payload.get("user_prompt") or "").strip()
        if not prompt:
            return {"continue": True}
        project = str(payload.get("project") or "generic")
        resp = self.runtime.activation.preflight_activate(
            PreflightRequest(
                task=prompt,
                project=project,
                client=client,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
        header = f"[ai-flywheel · receipt={resp.receipt_id} · mode={resp.anti_laziness_mode}]"
        return {
            "continue": True,
            "additionalContext": f"{header}\n{resp.compact_context}",
            "receipt_id": resp.receipt_id,
        }

    def _handle_pre_tool(
        self, client: str, session_id: str, turn_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
        tool_input = payload.get("tool_input") or payload.get("input") or {}
        if not tool_name or tool_name in READONLY_TOOLS:
            return {"continue": True}

        input_str = str(tool_input)
        lower_input = input_str.strip().lower()
        for prefix in READONLY_CMD_PREFIXES:
            if lower_input.startswith(prefix):
                return {"continue": True}

        hard_gates = self.runtime.policies.get_hard_gates()
        warnings: list[str] = []

        # 1. High-risk pattern warning
        for pattern in hard_gates.get("high_risk_patterns", []):
            if str(pattern).lower() in lower_input:
                msg = f"检测到高危命令模式 `{pattern}`（工具: {tool_name}），请先核对范围、备份与授权边界。"
                warnings.append(f"[ai-flywheel·高危观察] {msg}")
                self.runtime.receipts.report_signal(
                    ReportSignalRequest(
                        client=client,
                        session_id=session_id,
                        turn_id=turn_id,
                        kind=SignalKind.HIGH_RISK_TOOL,
                        severity="high",
                        summary=msg,
                        tool_name=tool_name,
                        detail={"pattern": pattern},
                    )
                )
                break

        # 2. Retry-loop breaker (default alert at >= 3 identical attempts in same turn)
        breaker = hard_gates.get("retry_breaker", {})
        if breaker.get("enabled", True):
            alert_at = int(breaker.get("alert_at_attempt", 3))
            fp = canonical_hash({"tool": tool_name, "input": input_str[:500]})[:16]
            counter_key = f"{client}:{session_id}:{turn_id}:{tool_name}:{fp}"
            count = self.runtime.db.bump_retry_counter(counter_key)
            if count >= alert_at:
                msg = (
                    f"同一工具 `{tool_name}` 相同输入在本轮已连续尝试 {count} 次，"
                    "触发反偷懒死循环保护：请停止盲目重试，改为检查根因、解释器或换用专用方案。"
                )
                warnings.append(f"[ai-flywheel·死循环保护] {msg}")
                self.runtime.receipts.report_signal(
                    ReportSignalRequest(
                        client=client,
                        session_id=session_id,
                        turn_id=turn_id,
                        kind=SignalKind.RETRY_LOOP,
                        severity="warning",
                        summary=msg,
                        tool_name=tool_name,
                        detail={"attempt": count},
                    )
                )

        if warnings:
            return {"continue": True, "additionalContext": "\n".join(warnings)}
        return {"continue": True}

    def _handle_session_lifecycle(
        self, client: str, event: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not session_id:
            return {"continue": True}
        transcript_path = payload.get("transcript_path") or payload.get("rollout_path")
        workspace = str(payload.get("cwd") or payload.get("workspace") or "")
        title = str(payload.get("title") or "")
        summary = payload.get("summary")
        self.runtime.register_session(
            RegisterSessionRequest(
                client=client,
                session_id=session_id,
                title=title,
                workspace=workspace,
                transcript_path=str(transcript_path) if transcript_path else None,
                summary=str(summary) if summary else None,
                metadata={"last_hook_event": event},
            )
        )
        return {"continue": True}
