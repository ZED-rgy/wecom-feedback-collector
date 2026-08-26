from __future__ import annotations

from typing import Protocol

from ..config import Settings


class WeComAccountSender(Protocol):
    def is_ready(self) -> bool:
        ...

    def send_text(self, room_id: str, content: str) -> None:
        ...


class DryRunSender:
    def is_ready(self) -> bool:
        return True

    def send_text(self, room_id: str, content: str) -> None:
        print(f"[dry-run] send to room={room_id}:\n{content}")


class DisabledSender:
    """Keep generated jobs pending until automatic account sending is enabled."""

    def is_ready(self) -> bool:
        return False

    def send_text(self, room_id: str, content: str) -> None:
        raise RuntimeError("automatic WeCom account sending is disabled")


class WindowsScheduledSender:
    """Send an already scheduled message through the signed-in desktop account.

    Enabling this adapter is an explicit, persistent operator authorization in
    the local dashboard.  The underlying UI adapter still prepares and verifies
    the exact payload before the final Enter key is emitted.
    """

    def __init__(self, group_name: str, group_remark: str = ""):
        from .windows_ui import WindowsUiConfig, WindowsWeComUiSender

        self._sender = WindowsWeComUiSender(
            WindowsUiConfig(group_name=group_name, group_remark=group_remark)
        )

    def is_ready(self) -> bool:
        return self._sender.is_ready()

    def send_text(self, room_id: str, content: str) -> None:
        self._sender.prepare_text(room_id, content)
        self._sender.confirm_and_send(room_id, content, confirmed=True)


def build_sender(settings: Settings) -> WeComAccountSender:
    if settings.dry_run:
        return DryRunSender()
    if not settings.auto_send_enabled:
        return DisabledSender()
    if not settings.target_group_name:
        return DisabledSender()
    return WindowsScheduledSender(settings.target_group_name, settings.target_group_remark)


def build_manual_sender(settings: Settings) -> WeComAccountSender:
    """Build a sender for an operator-confirmed "send now" action."""
    if settings.dry_run:
        return DryRunSender()
    if not settings.target_group_name:
        return DisabledSender()
    return WindowsScheduledSender(settings.target_group_name, settings.target_group_remark)
