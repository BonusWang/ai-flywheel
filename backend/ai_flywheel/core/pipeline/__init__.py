"""The P00–P07 pipeline package.

The public import surface stays :mod:`ai_flywheel.core.orchestrator`; this package
holds the implementation split by concern:

- ``constants``  — node registry, source labels and the pipeline's errors;
- ``events``     — the in-process SSE broker;
- ``git_commit`` — the optional P04 post-write local Git trail;
- ``helpers``    — candidate normalisation, promotion and routing helpers;
- ``recovery``   — heartbeat, lock checkpoints and the terminal paths;
- ``coordinator``— the state machine that assembles the stages.
"""
from ai_flywheel.core.pipeline.constants import (
    LANDING_FAILED_REASON,
    NODE_TEMPLATES,
    PLAN_SOURCE_LABELS,
    REVIEW_SOURCE_LABELS,
    ApprovalLockBusy,
    LockLostError,
    WorkflowLockError,
)
from ai_flywheel.core.pipeline.events import EventBroker
from ai_flywheel.core.pipeline.git_commit import commit_p04_written_files
from ai_flywheel.core.pipeline.coordinator import WorkflowOrchestrator

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
    "commit_p04_written_files",
]
