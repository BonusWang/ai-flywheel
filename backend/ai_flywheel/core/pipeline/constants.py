"""Fixed registry, labels and errors shared by every P00–P07 pipeline stage.

``NODE_TEMPLATES`` is the single list of node ids: a run is built from it and the
portal labels cover exactly these ids. The source labels name the planner /
reviewer that produced a plan or a review, so a node never claims a source the
runner did not declare.
"""
from __future__ import annotations

from typing import Any

from ai_flywheel.adapters.runners import (
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_HEURISTIC,
    PLAN_SOURCE_MODEL,
    REVIEW_SOURCE_CLI,
    REVIEW_SOURCE_HEURISTIC,
    REVIEW_SOURCE_MODEL,
    REVIEW_SOURCE_UNDECLARED,
)
from ai_flywheel.core.approval_channel import ApprovalBusyError

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

# A routed document the landing adapter could not turn into a page. Not expected: the route
# verdict already guarantees a page type the adapter can materialise.
LANDING_FAILED_REASON = "路由通过，但落点适配未能生成页面"


class WorkflowLockError(RuntimeError):
    """Raised when an active workflow run already holds the exclusive lock."""


class ApprovalLockBusy(ApprovalBusyError):
    """Raised when an approval claim fails because another run holds an unexpired lock.

    Carries the holder's run id and the lock's expiry so callers can surface a
    meaningful retry hint (portal maps this to HTTP 409). It subclasses the
    approval-channel contract's busy error, so an approval channel can recognise
    the outcome without importing the pipeline package.
    """

    def __init__(self, message: str, *, lock_run_id: str | None, lock_expires_at: str | None) -> None:
        super().__init__(message, lock_run_id=lock_run_id, lock_expires_at=lock_expires_at)


class LockLostError(RuntimeError):
    """Raised at a synchronous checkpoint when the lock is gone or no longer ours."""


def _error_payload_with_telemetry(exc: Exception) -> dict[str, Any]:
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


# The name this helper had inside the pre-refactor ``core.orchestrator`` module.
# Kept so an out-of-tree import of the private name keeps resolving.
_model_call_failure_payload = _error_payload_with_telemetry
