"""Backward-compatible import shim for the Feishu card renderer.

The implementation lives in :mod:`ai_flywheel.adapters.channels.feishu.card`
since ``refactor-approval-channel-abstraction``. This module re-exports the same
objects so existing imports keep working; new code should import the adapter.
"""
from ai_flywheel.adapters.channels.feishu.card import (  # noqa: F401
    ACTION_LABEL,
    APPROVAL_MODE_LABEL,
    CARD_BYTES_BUDGET,
    FULLWIDTH_MAP,
    KIND_LABEL,
    LEVELS,
    LINK_SCHEME_RE,
    OPTION_APPROVE_ALL,
    OPTION_APPROVE_ROUTINE,
    OPTION_REJECT,
    PRIVATE_RE,
    SECRET_MASK,
    callback_button,
    is_sensitive,
    render_ask_card,
    render_message_card,
    render_state_card,
    sanitize,
    sha12,
    with_notice,
)

__all__ = [
    "ACTION_LABEL",
    "APPROVAL_MODE_LABEL",
    "CARD_BYTES_BUDGET",
    "FULLWIDTH_MAP",
    "KIND_LABEL",
    "LEVELS",
    "LINK_SCHEME_RE",
    "OPTION_APPROVE_ALL",
    "OPTION_APPROVE_ROUTINE",
    "OPTION_REJECT",
    "PRIVATE_RE",
    "SECRET_MASK",
    "callback_button",
    "is_sensitive",
    "render_ask_card",
    "render_message_card",
    "render_state_card",
    "sanitize",
    "sha12",
    "with_notice",
]
