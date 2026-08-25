from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("wecom_feedback.windows_ui")


class WindowsUiError(RuntimeError):
    pass


class ConfirmationRequired(WindowsUiError):
    """Raised when a caller tries to send without explicit confirmation."""


@dataclass(frozen=True)
class WindowsUiConfig:
    window_title: str = "企业微信"
    # Points are relative to the top-left of the WeCom window.
    search_point: tuple[int, int] = (270, 40)
    editor_point: tuple[int, int] = (720, 910)
    group_name: str = ""
    settle_seconds: float = 0.8


def _pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as exc:  # pragma: no cover - depends on Windows environment
        raise WindowsUiError("Windows UI adapter requires: pip install -e \".[windows]\"") from exc
    pyautogui.PAUSE = 0.15
    return pyautogui


def _paste_text(text: str) -> None:
    try:
        import pyperclip
    except ImportError as exc:  # pragma: no cover - depends on Windows environment
        raise WindowsUiError("Windows UI adapter requires: pip install -e \".[windows]\"") from exc
    pyperclip.copy(text)
    _pyautogui().hotkey("ctrl", "v")


def _visible_window_by_title(title: str) -> int | None:
    """Find a visible top-level window without using a process hook."""
    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    get_title = user32.GetWindowTextW
    is_visible = user32.IsWindowVisible
    result: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not is_visible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(256)
        get_title(hwnd, buffer, 256)
        if buffer.value.strip().lower() == title.strip().lower():
            result.append(int(hwnd))
        return True

    enum_windows(enum_windows_proc(callback), 0)
    if len(result) > 1:
        raise WindowsUiError(f"found multiple windows titled {title!r}")
    return result[0] if result else None


def _window_origin(hwnd: int) -> tuple[int, int]:
    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise WindowsUiError("cannot read WeCom window bounds")
    return int(rect.left), int(rect.top)


class WindowsWeComUiSender:
    """Coordinate-based sender with a mandatory prepare/confirm boundary.

    WeCom's DirectUI surface is not consistently exposed through UI Automation.
    This first adapter therefore keeps coordinates configurable and never sends
    unless `confirm_and_send` is called explicitly by an operator-controlled flow.
    """

    def __init__(self, config: WindowsUiConfig):
        self.config = config
        self._prepared: tuple[str, str] | None = None

    def is_ready(self) -> bool:
        return _visible_window_by_title(self.config.window_title) is not None

    def prepare_text(self, room_id: str, content: str) -> None:
        if not self.config.group_name:
            raise WindowsUiError("group_name is required for UI-based sending")
        hwnd = _visible_window_by_title(self.config.window_title)
        if hwnd is None:
            raise WindowsUiError("WeCom window is not visible; unlock and open WeCom first")
        pyautogui = _pyautogui()
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow(hwnd)
        time.sleep(self.config.settle_seconds)
        origin_x, origin_y = _window_origin(hwnd)
        search_x, search_y = self.config.search_point
        editor_x, editor_y = self.config.editor_point
        pyautogui.click(origin_x + search_x, origin_y + search_y)
        pyautogui.hotkey("ctrl", "a")
        _paste_text(self.config.group_name)
        pyautogui.press("enter")
        time.sleep(self.config.settle_seconds)
        pyautogui.click(origin_x + editor_x, origin_y + editor_y)
        _paste_text(content)
        self._prepared = (room_id, content)

    def send_text(self, room_id: str, content: str) -> None:
        raise ConfirmationRequired(
            "message prepared but not sent; call confirm_and_send after visual verification"
        )

    def confirm_and_send(self, room_id: str, content: str, confirmed: bool = False) -> None:
        if not confirmed:
            raise ConfirmationRequired("explicit confirmed=True is required to send")
        if self._prepared != (room_id, content):
            self.prepare_text(room_id, content)
        _pyautogui().press("enter")
        self._prepared = None
