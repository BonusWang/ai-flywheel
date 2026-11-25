"""Backward-compatible import shim for the Feishu approval channel.

The implementation lives in :mod:`ai_flywheel.adapters.channels.feishu.channel`
since ``refactor-approval-channel-abstraction``. This module re-exports the same
objects so existing imports keep working; new code should import the adapter.
"""
from ai_flywheel.adapters.channels.feishu.channel import (  # noqa: F401
    DEFERRAL_SECONDS,
    OPTION_ACTIONS,
    FeishuChannel,
)

__all__ = ["DEFERRAL_SECONDS", "OPTION_ACTIONS", "FeishuChannel"]
