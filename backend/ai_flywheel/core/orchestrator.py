"""Public facade of the P00–P07 pipeline.

The implementation lives in :mod:`ai_flywheel.core.pipeline` since
``refactor-orchestrator-pipeline-pkg``. This module re-exports the same objects
under the historical import path, so every existing import keeps working.

It is also the documented seam for replacing a collaborator: the pipeline
resolves ``build_runner`` / ``run_dream_consolidation`` / ``rebuild_core_card``
through this module at call time, so a monkeypatch aimed here still takes effect.
"""
from __future__ import annotations

# --- the pipeline's public API -------------------------------------------------
from ai_flywheel.core.pipeline.constants import (  # noqa: F401
    LANDING_FAILED_REASON,
    NODE_TEMPLATES,
    PLAN_SOURCE_LABELS,
    REVIEW_SOURCE_LABELS,
    ApprovalLockBusy,
    LockLostError,
    WorkflowLockError,
    _error_payload_with_telemetry,
    _model_call_failure_payload,
)
from ai_flywheel.core.pipeline.coordinator import WorkflowOrchestrator  # noqa: F401
from ai_flywheel.core.pipeline.events import EventBroker  # noqa: F401
from ai_flywheel.core.pipeline.git_commit import commit_p04_written_files  # noqa: F401

# --- collaborators the pipeline resolves through this module at call time -------
from ai_flywheel.adapters.runners import build_runner  # noqa: F401
from ai_flywheel.core.core_card import rebuild_core_card  # noqa: F401
from ai_flywheel.core.dream import run_dream_consolidation  # noqa: F401

__all__ = [
    "LANDING_FAILED_REASON",
    "NODE_TEMPLATES",
    "PLAN_SOURCE_LABELS",
    "REVIEW_SOURCE_LABELS",
    "ApprovalLockBusy",
    "EventBroker",
    "LockLostError",
    "WorkflowLockError",
    "WorkflowOrchestrator",
    "build_runner",
    "commit_p04_written_files",
    "rebuild_core_card",
    "run_dream_consolidation",
]
