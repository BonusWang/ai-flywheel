"""审批通道契约（refactor-approval-channel-abstraction）。

编排器与运行时只依赖这里声明的能力，不依赖任何具体通道实现：

- ``availability``：通道当前是否可用（``ok`` / ``transient:*`` / ``down:*``）；
- ``first_send``：为一张提问卡完成首次投递（交互卡片或文本退回）；
- ``handle_click``：受理一次来自通道的点击事件；
- ``start`` / ``stop``：随 ``serve`` 生命周期启停发件箱与回写线程；
- ``tick``：执行一轮补投、重试与状态回写。

``ApprovalBusyError`` 是契约级的「另一批次持锁」结果：编排器的
``ApprovalLockBusy`` 继承它，通道只捕获基类，因此适配器不必反向导入编排器。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ApprovalBusyError(RuntimeError):
    """The approval continuation could not claim the exclusive workflow lock.

    Carries the holder's run id and the lock's expiry so a channel can keep the
    card open and tell the approver to try again later.
    """

    def __init__(
        self,
        message: str,
        *,
        lock_run_id: str | None = None,
        lock_expires_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.lock_run_id = lock_run_id
        self.lock_expires_at = lock_expires_at


@runtime_checkable
class ApprovalChannel(Protocol):
    """The capabilities the orchestrator and the runtime require from a channel."""

    def availability(self) -> str:
        """``ok`` when the channel can deliver interactive cards right now."""
        ...

    def first_send(self, *, card_id: str, run: Any, reasons: list[str], portal_url: str) -> None:
        """Deliver the ask card for a run that just paused, or degrade to text."""
        ...

    def handle_click(self, event: dict[str, Any]) -> None:
        """Validate and accept one click delivered by the channel."""
        ...

    def start(self) -> None:
        """Start the channel's background delivery / sync loop."""
        ...

    def stop(self) -> None:
        """Stop the channel and release its consumer process."""
        ...

    def tick(self) -> None:
        """Run one delivery, retry and state-sync pass."""
        ...
