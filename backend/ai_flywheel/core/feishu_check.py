"""Backward-compatible import shim for the Feishu self-check service.

The implementation lives in :mod:`ai_flywheel.adapters.channels.feishu.check`
since ``refactor-approval-channel-abstraction``. This module re-exports the same
objects so existing imports keep working; new code should import the adapter.
"""
from ai_flywheel.adapters.channels.feishu.check import (  # noqa: F401
    CHECK_RUN_ID,
    CHECK_SETTING_KEY,
    FeishuCheckService,
    event_card_id,
    render_check_card,
)

__all__ = [
    "CHECK_RUN_ID",
    "CHECK_SETTING_KEY",
    "FeishuCheckService",
    "event_card_id",
    "render_check_card",
]
