from __future__ import annotations

from datetime import datetime, timezone
import sys
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
        output = f"[dry-run] send to room={room_id}:\n{content}"
        try:
            print(output)
        except UnicodeEncodeError:
            # GitHub Windows runners and some legacy terminals use cp1252 or
            # another narrow code page.  Dry-run diagnostics must never turn
            # a successful test into a failed send because of console output.
            encoding = sys.stdout.encoding or "utf-8"
            print(output.encode(encoding, errors="replace").decode(encoding))


class DisabledSender:
    """Keep generated jobs pending until automatic account sending is enabled."""

    def is_ready(self) -> bool:
        return False

    def send_text(self, room_id: str, content: str) -> None:
        raise RuntimeError("automatic WeCom account sending is disabled")


class DeliveryUnconfirmed(RuntimeError):
    """Enter was emitted, but the target group database did not confirm delivery."""


class WindowsScheduledSender:
    """Send an already scheduled message through the signed-in desktop account.

    Enabling this adapter is an explicit, persistent operator authorization in
    the local dashboard.  The underlying UI adapter still prepares and verifies
    the exact payload before the final Enter key is emitted.
    """

    def __init__(self, settings: Settings):
        from .windows_ui import WindowsUiConfig, WindowsWeComUiSender
        from .windows_local_db import WindowsWeComLocalDbReceiver

        self._sender = WindowsWeComUiSender(
            WindowsUiConfig(group_name=settings.target_group_name, group_remark=settings.target_group_remark)
        )
        self._verifier = WindowsWeComLocalDbReceiver(settings, lambda _message: False)

    def is_ready(self) -> bool:
        return self._sender.is_ready()

    def send_text(self, room_id: str, content: str) -> None:
        self._sender.prepare_text(room_id, content)
        started_at = datetime.now(timezone.utc)
        self._sender.confirm_and_send(room_id, content, confirmed=True)
        if not self._verifier.wait_for_message(content, started_at):
            raise DeliveryUnconfirmed(
                "已执行发送，但未在目标群本地消息库中确认；任务不会自动重试，请人工核对"
            )


def build_sender(settings: Settings) -> WeComAccountSender:
    if settings.dry_run:
        return DryRunSender()
    if not settings.auto_send_enabled:
        return DisabledSender()
    if not settings.target_group_name:
        return DisabledSender()
    return WindowsScheduledSender(settings)


def build_manual_sender(settings: Settings) -> WeComAccountSender:
    """Build a sender for an operator-confirmed "send now" action."""
    if settings.dry_run:
        return DryRunSender()
    if not settings.target_group_name:
        return DisabledSender()
    return WindowsScheduledSender(settings)
