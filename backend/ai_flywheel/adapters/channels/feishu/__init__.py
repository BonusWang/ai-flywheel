"""飞书审批通道实现：卡片渲染、通道协调、点击监听与链路自检。"""
from __future__ import annotations

from ai_flywheel.adapters.channels.feishu.card import (
    render_ask_card,
    render_message_card,
    render_state_card,
    sanitize,
    with_notice,
)
from ai_flywheel.adapters.channels.feishu.channel import FeishuChannel
from ai_flywheel.adapters.channels.feishu.check import FeishuCheckService
from ai_flywheel.adapters.channels.feishu.listen import FeishuClickListener

__all__ = [
    "FeishuChannel",
    "FeishuCheckService",
    "FeishuClickListener",
    "render_ask_card",
    "render_message_card",
    "render_state_card",
    "sanitize",
    "with_notice",
]
