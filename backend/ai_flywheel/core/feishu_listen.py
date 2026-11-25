"""Backward-compatible import shim for the Feishu click listener.

The implementation lives in :mod:`ai_flywheel.adapters.channels.feishu.listen`
since ``refactor-approval-channel-abstraction``. This module re-exports the same
objects so existing imports keep working; new code should import the adapter.
"""
from ai_flywheel.adapters.channels.feishu.listen import (  # noqa: F401
    READY_MARKER,
    FeishuClickListener,
    classify_failure,
)

__all__ = ["READY_MARKER", "FeishuClickListener", "classify_failure"]
