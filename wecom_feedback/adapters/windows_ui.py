from __future__ import annotations

import ctypes
import logging
import re
import secrets
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
    group_name: str = ""
    group_remark: str = ""
    settle_seconds: float = 0.8
    ocr_min_confidence: float = 0.65


def _pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as exc:  # pragma: no cover - depends on Windows environment
        raise WindowsUiError("Windows UI adapter requires: pip install -e \".[windows]\"") from exc
    pyautogui.PAUSE = 0.15
    return pyautogui


def _clipboard() -> Any:
    try:
        import pyperclip
    except ImportError as exc:  # pragma: no cover - depends on Windows environment
        raise WindowsUiError("Windows UI adapter requires: pip install -e \".[windows]\"") from exc
    return pyperclip


def _paste_text(text: str) -> None:
    _clipboard().copy(text)
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


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise WindowsUiError("cannot read WeCom window bounds")
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _foreground_window() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow())


def _normalized_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def _activate_window(hwnd: int, settle_seconds: float) -> None:
    """Bring WeCom to the foreground and fail closed if Windows refuses."""
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    for _ in range(3):
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(min(max(settle_seconds / 3, 0.08), 0.3))
        if _foreground_window() == hwnd:
            return
    raise WindowsUiError("无法将企微窗口切换到前台，已禁止发送")


def _focus_target_group(hwnd: int, config: WindowsUiConfig) -> None:
    """Use WeCom's keyboard search to focus the configured group's editor."""
    pyautogui = _pyautogui()
    pyautogui.press("esc", presses=3, interval=0.08)
    pyautogui.hotkey("ctrl", "f")
    _paste_text(config.group_remark or config.group_name)
    pyautogui.press("enter")
    time.sleep(config.settle_seconds)
    _verify_group_header(hwnd, config.group_name, config.ocr_min_confidence)


def _read_focused_editor_text() -> str | None:
    """Copy the focused editor without trusting a stale clipboard value."""
    pyautogui = _pyautogui()
    clipboard = _clipboard()
    sentinel = f"WECOM-COPY-CHECK-{secrets.token_hex(12)}"
    clipboard.copy(sentinel)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.2)
    copied = str(clipboard.paste())
    if copied == sentinel:
        pyautogui.press("esc")
        return None
    pyautogui.press("right")  # Collapse the selection without changing text.
    return copied


_OCR_ENGINE: Any = None


def _verify_group_header(hwnd: int, group_name: str, min_confidence: float) -> None:
    """OCR only the chat header and require the configured group name."""
    global _OCR_ENGINE
    try:
        import numpy as np
        import pyautogui
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - Windows-only dependencies
        raise WindowsUiError("安全发送需要 rapidocr-onnxruntime 以核对目标群") from exc
    if _foreground_window() != hwnd:
        raise WindowsUiError("企微窗口已失去焦点，已终止发送")
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    header_left = left + int(width * 0.26)
    header_width = max(260, int(width * 0.46))
    header_height = min(90, max(65, int(height * 0.09)))
    screenshot = np.asarray(pyautogui.screenshot(region=(header_left, top, header_width, header_height)))
    result, _ = _OCR_ENGINE(screenshot)
    texts = [
        str(row[1]).strip()
        for row in (result or [])
        if len(row) >= 3 and float(row[2]) >= min_confidence
    ]
    expected = re.sub(r"\s+", "", group_name).lower()
    observed = "".join(re.sub(r"\s+", "", text).lower() for text in texts)
    if not expected or expected not in observed:
        raise WindowsUiError(f"目标群校验失败：期望“{group_name}”，窗口标题识别为“{' / '.join(texts) or '空'}”")


class WindowsWeComUiSender:
    """Keyboard-first sender with target, editor and payload verification."""

    def __init__(self, config: WindowsUiConfig):
        self.config = config
        self._prepared: tuple[str, str] | None = None
        self._prepared_hwnd: int | None = None

    def is_ready(self) -> bool:
        return _visible_window_by_title(self.config.window_title) is not None

    def prepare_text(self, room_id: str, content: str) -> None:
        if not self.config.group_name:
            raise WindowsUiError("group_name is required for UI-based sending")
        hwnd = _visible_window_by_title(self.config.window_title)
        if hwnd is None:
            raise WindowsUiError("WeCom window is not visible; unlock and open WeCom first")
        pyautogui = _pyautogui()
        # Use a deterministic layout. This removes the old dependency on a
        # particular window position, size, DPI scale, or remembered placement.
        _activate_window(hwnd, self.config.settle_seconds)
        clipboard = _clipboard()
        original_clipboard = clipboard.paste()
        try:
            # Close transient menus/modals, then use WeCom's own keyboard search
            # instead of clicking a coordinate in the sidebar.
            _focus_target_group(hwnd, self.config)

            # Entering an exact search result moves focus to WeCom's message
            # editor. Do not click anywhere: DirectUI toolbar positions change
            # with window size and previously caused the payment action to open.
            draft_sentinel = f"WECOM-DRAFT-CHECK-{secrets.token_hex(12)}"
            clipboard.copy(draft_sentinel)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.2)
            existing_draft = str(clipboard.paste())
            if existing_draft != draft_sentinel and _normalized_text(existing_draft):
                pyautogui.press("right")
                raise WindowsUiError("目标群输入框中已有未发送草稿，已保留草稿并终止自动发送")
            pyautogui.press("backspace")
            _paste_text(content)
            time.sleep(0.2)

            # A stale clipboard value could make a failed Ctrl+C look valid.
            # Replace it with a random sentinel first, then require an exact
            # copy-back from the focused editor before Enter is ever allowed.
            copied = _read_focused_editor_text()
            if copied is None or _normalized_text(copied) != _normalized_text(content):
                raise WindowsUiError("输入框内容回读失败，可能点到了工具栏或弹窗，已禁止发送")
            self._prepared = (room_id, content)
            self._prepared_hwnd = hwnd
        finally:
            clipboard.copy(original_clipboard)

    def send_text(self, room_id: str, content: str) -> None:
        raise ConfirmationRequired(
            "message prepared but not sent; call confirm_and_send after visual verification"
        )

    def confirm_and_send(self, room_id: str, content: str, confirmed: bool = False) -> None:
        if not confirmed:
            raise ConfirmationRequired("explicit confirmed=True is required to send")
        if self._prepared != (room_id, content):
            self.prepare_text(room_id, content)
        hwnd = self._prepared_hwnd
        if hwnd is None or _visible_window_by_title(self.config.window_title) != hwnd:
            raise WindowsUiError("企微窗口已变化，已禁止发送")
        clipboard = _clipboard()
        original_clipboard = clipboard.paste()
        try:
            # The desktop control panel can regain focus while the HTTP request
            # is still running. Reacquire WeCom and revalidate both destination
            # and payload immediately before Enter instead of trusting focus
            # retained from prepare_text().
            _activate_window(hwnd, self.config.settle_seconds)
            _focus_target_group(hwnd, self.config)
            copied = _read_focused_editor_text()
            if copied is None or _normalized_text(copied) != _normalized_text(content):
                raise WindowsUiError("发送前输入框内容校验失败，已禁止发送")
            _activate_window(hwnd, 0.15)
            if _foreground_window() != hwnd:
                raise WindowsUiError("企微窗口再次失去焦点，已禁止发送")
            _verify_group_header(hwnd, self.config.group_name, self.config.ocr_min_confidence)
            _pyautogui().press("enter")
            self._prepared = None
            self._prepared_hwnd = None
        finally:
            clipboard.copy(original_clipboard)
