from __future__ import annotations

"""Experimental inbound reader for the logged-in Windows WeCom client.

This adapter reads text exposed by the currently open chat window through
Windows UI Automation. It does not inject into WeCom or bypass verification.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings
from ..models import RawMessage
from .windows_ui import WindowsUiError

logger = logging.getLogger("wecom_feedback.windows_ui_receiver")


@dataclass(frozen=True)
class WindowsUiReceiverConfig:
    window_title: str = "企业微信"
    poll_seconds: float = 2.0
    max_control_depth: int = 8


def _desktop_window(title: str) -> Any:
    try:
        from pywinauto import Desktop
    except ImportError as exc:  # pragma: no cover - Windows-only dependency
        raise WindowsUiError("UI接收需要 pywinauto，请执行: pip install -e \".[windows]\"") from exc
    try:
        window = Desktop(backend="uia").window(title=title)
        if not window.exists(timeout=0.5):
            raise WindowsUiError(f"未找到企微窗口: {title}")
        return window
    except Exception as exc:
        if isinstance(exc, WindowsUiError):
            raise
        raise WindowsUiError(f"无法访问企微窗口: {exc}") from exc


def _visible_texts(window: Any, max_depth: int) -> list[str]:
    try:
        controls = window.descendants(depth=max_depth)
    except TypeError:
        controls = window.descendants()
    texts: list[str] = []
    for control in controls:
        try:
            text = str(control.window_text() or "").strip()
        except Exception:
            continue
        if text and text not in texts:
            texts.append(text)
    return texts


class WindowsWeComUiReceiver:
    """Poll the currently open target group and emit newly seen @mentions."""

    def __init__(
        self,
        settings: Settings,
        on_message: Callable[[RawMessage], None],
        config: WindowsUiReceiverConfig | None = None,
    ):
        self.settings = settings
        self.on_message = on_message
        self.config = config or WindowsUiReceiverConfig()
        self._seen: set[str] = set()
        self._stop = False

    def is_ready(self) -> bool:
        try:
            _desktop_window(self.config.window_title)
        except WindowsUiError:
            return False
        return True

    def poll_once(self) -> int:
        window = _desktop_window(self.config.window_title)
        texts = _visible_texts(window, self.config.max_control_depth)
        target_names = tuple(name.lower() for name in self.settings.target_account_names)
        accepted = 0
        for text in texts:
            normalized = text.replace("\u2005", " ").strip()
            if not normalized or not any(f"@{name}" in normalized.lower() for name in target_names):
                continue
            digest = hashlib.sha1(
                f"{self.settings.target_group_name}\n{normalized}".encode("utf-8")
            ).hexdigest()
            if digest in self._seen:
                continue
            self._seen.add(digest)
            self.on_message(
                RawMessage(
                    message_id=f"ui-{digest[:20]}",
                    seq=0,
                    account_id="ui-customer",
                    room_id=self.settings.target_room_id or self.settings.target_group_name,
                    group_name=self.settings.target_group_name,
                    group_remark=self.settings.target_group_remark,
                    sender_id="ui-unknown",
                    sender_name="界面识别",
                    message_type="text",
                    raw_content=normalized,
                    content=normalized,
                    mentioned_account=True,
                )
            )
            accepted += 1
        return accepted

    def run_forever(self) -> None:
        self._stop = False
        logger.info("UI receiver started; keep the target WeCom group open")
        while not self._stop:
            try:
                count = self.poll_once()
                if count:
                    logger.info("UI receiver accepted %s new mention(s)", count)
            except WindowsUiError as exc:
                logger.warning("UI receiver paused: %s", exc)
            except Exception:
                logger.exception("UI receiver poll failed")
            time.sleep(max(0.5, self.config.poll_seconds))

    def stop(self) -> None:
        self._stop = True
