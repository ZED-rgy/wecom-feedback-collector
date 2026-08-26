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
    control_window_title: str = "企微反馈收集控制台"
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


_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_VK_BACK = 0x08
_VK_RETURN = 0x0D
_VK_SHIFT = 0x10
_VK_RIGHT = 0x27


def _send_window_key(hwnd: int, virtual_key: int) -> None:
    """Send a key only to WeCom instead of the global foreground app."""
    user32 = ctypes.windll.user32
    user32.SendMessageW(hwnd, _WM_KEYDOWN, virtual_key, 0)
    user32.SendMessageW(hwnd, _WM_KEYUP, virtual_key, 0xC0000001)


def _send_window_chord(hwnd: int, modifier: int, virtual_key: int) -> None:
    user32 = ctypes.windll.user32
    user32.SendMessageW(hwnd, _WM_KEYDOWN, modifier, 0)
    user32.SendMessageW(hwnd, _WM_KEYDOWN, virtual_key, 0)
    user32.SendMessageW(hwnd, _WM_KEYUP, virtual_key, 0xC0000001)
    user32.SendMessageW(hwnd, _WM_KEYUP, modifier, 0xC0000001)


def _send_input_key_events(
    hwnd: int,
    events: list[tuple[int, bool]],
    settle_seconds: float,
) -> None:
    """Atomically inject non-text keys after verifying WeCom foreground."""
    from ctypes import wintypes

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeyboardInput), ("mi", MouseInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("data",)
        _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

    def key_event(virtual_key: int, key_up: bool) -> Input:
        return Input(
            type=1,
            ki=KeyboardInput(virtual_key, 0, 0x0002 if key_up else 0, 0, 0),
        )

    _activate_window(hwnd, settle_seconds)
    if _foreground_window() != hwnd:
        raise WindowsUiError("企微窗口已失去焦点，无法安全执行按键")
    input_events = [key_event(virtual_key, key_up) for virtual_key, key_up in events]
    input_array = (Input * len(input_events))(*input_events)
    sent = int(ctypes.windll.user32.SendInput(len(input_array), input_array, ctypes.sizeof(Input)))
    if sent != len(input_events):
        raise WindowsUiError("企微安全按键未完整执行，已终止发送")


def _send_window_text(hwnd: int, text: str, settle_seconds: float = 0.15) -> None:
    """Write Unicode text to WeCom's focused DirectUI control.

    Newlines are inserted with Shift+Enter so they cannot accidentally trigger
    the chat editor's send action.
    """
    user32 = ctypes.windll.user32
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for chunk_index, chunk in enumerate(normalized.split("\n")):
        if chunk_index:
            # Shift+Enter must be a real input event because WeCom checks the
            # physical modifier state. It contains no user text and is sent as
            # one atomic batch immediately after a foreground assertion.
            _send_input_key_events(
                hwnd,
                [(_VK_SHIFT, False), (_VK_RETURN, False), (_VK_RETURN, True), (_VK_SHIFT, True)],
                settle_seconds,
            )
            # SendInput is queued while the following WM_CHAR calls are
            # synchronous; allow the line break to be consumed first.
            time.sleep(0.08)
        encoded = chunk.encode("utf-16-le", errors="surrogatepass")
        for offset in range(0, len(encoded), 2):
            code_unit = int.from_bytes(encoded[offset : offset + 2], "little")
            user32.SendMessageW(hwnd, _WM_CHAR, code_unit, 1)


def _send_select_all_copy(hwnd: int, settle_seconds: float) -> None:
    """Atomically select and copy from the verified foreground WeCom window.

    WeCom's editor is a custom DirectUI control and ignores WM_COPY. SendInput
    is therefore retained only for this non-destructive readback operation. No
    user text or Enter key is ever sent through the global input queue.
    """
    events: list[tuple[int, bool]] = []
    for virtual_key in (0x41, 0x43):  # Ctrl+A, Ctrl+C
        events.extend(
            [
                (0x11, False),
                (virtual_key, False),
                (virtual_key, True),
                (0x11, True),
            ]
        )
    _send_input_key_events(hwnd, events, settle_seconds)


def _paste_window_text(hwnd: int, text: str, settle_seconds: float) -> None:
    """Paste the prepared message into the already verified WeCom editor."""
    clipboard = _clipboard()
    clipboard.copy(text)
    _send_input_key_events(
        hwnd,
        [
            (0x11, False),  # Ctrl down
            (0x56, False),  # V down
            (0x56, True),
            (0x11, True),
        ],
        settle_seconds,
    )


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


def _window_process_id(hwnd: int) -> int:
    process_id = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return int(process_id.value)


def _belongs_to_same_process(candidate_hwnd: int, target_hwnd: int) -> bool:
    candidate_pid = _window_process_id(candidate_hwnd)
    target_pid = _window_process_id(target_hwnd)
    return bool(candidate_pid and candidate_pid == target_pid)


def _normalized_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def _activate_window(hwnd: int, settle_seconds: float) -> None:
    """Bring WeCom to the foreground and fail closed if Windows refuses."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    for _ in range(3):
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground = _foreground_window()
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None))
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached_threads: list[int] = []
        try:
            # The sender usually runs from a background worker two seconds
            # after the WebView click. Attach to the active input queues so
            # Windows does not reject SetForegroundWindow merely because that
            # worker was not the process that received the user's last click.
            for thread_id in {foreground_thread, target_thread}:
                if thread_id and thread_id != current_thread:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached_threads.append(thread_id)
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for thread_id in attached_threads:
                user32.AttachThreadInput(current_thread, thread_id, False)
        time.sleep(min(max(settle_seconds / 3, 0.08), 0.3))
        if _foreground_window() == hwnd:
            # A WebView click can briefly hand focus back to the control panel
            # after SetForegroundWindow succeeds. Require a stable foreground
            # window instead of accepting that transient state.
            time.sleep(0.18)
            if _foreground_window() == hwnd:
                return
    raise WindowsUiError("无法将企微窗口切换到前台，已禁止发送")


def _minimize_control_window(title: str, target_hwnd: int) -> int | None:
    """Remove the desktop console from focus competition while sending."""
    hwnd = _visible_window_by_title(title)
    if hwnd is None or hwnd == target_hwnd:
        return None
    ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    time.sleep(0.25)
    logger.info("control window minimized during WeCom send: hwnd=%s", hwnd)
    return hwnd


def _restore_control_window(hwnd: int | None) -> None:
    if hwnd is None:
        return
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    logger.info("control window restored after WeCom send: hwnd=%s", hwnd)


def _click_wecom_area(hwnd: int, x_ratio: float, y_ratio: float, settle_seconds: float) -> None:
    """Focus a WeCom DirectUI area with a guarded physical mouse click."""
    _activate_window(hwnd, settle_seconds)
    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    _pyautogui().click(left + int(width * x_ratio), top + int(height * y_ratio))
    time.sleep(0.16)
    if _foreground_window() != hwnd:
        raise WindowsUiError("企微窗口在点击后失去焦点，已终止操作")


def _focus_target_group(hwnd: int, config: WindowsUiConfig) -> None:
    """Search and select the configured group without globally typing its name."""
    query = config.group_remark or config.group_name
    for attempt in range(2):
        # WeCom's search field is a custom DirectUI control. A guarded click is
        # needed only to assign its internal focus; all following text and keys
        # are posted directly to the known WeCom HWND.
        _click_wecom_area(hwnd, 0.135, 0.037, config.settle_seconds)
        for _ in range(80):
            _send_window_key(hwnd, _VK_BACK)
        _send_window_text(hwnd, query)
        time.sleep(0.25)
        try:
            _verify_search_query(hwnd, query, config.ocr_min_confidence)
        except WindowsUiError:
            _send_window_key(hwnd, 0x1B)  # Escape; never Enter an unverified field.
            raise
        _send_window_key(hwnd, _VK_RETURN)
        time.sleep(config.settle_seconds)
        try:
            _verify_group_header(hwnd, config.group_name, config.ocr_min_confidence)
            return
        except WindowsUiError as exc:
            if attempt or "失去焦点" not in str(exc):
                raise
            logger.warning("WeCom lost focus during group search; retrying once")
    raise WindowsUiError("企微群聊定位失败，已禁止发送")


def _focus_editor(hwnd: int, config: WindowsUiConfig) -> None:
    _verify_group_header(hwnd, config.group_name, config.ocr_min_confidence)
    _click_wecom_area(hwnd, 0.58, 0.93, config.settle_seconds)


def _read_focused_editor_text(hwnd: int, settle_seconds: float) -> str | None:
    """Copy the focused editor without trusting a stale clipboard value."""
    clipboard = _clipboard()
    sentinel = f"WECOM-COPY-CHECK-{secrets.token_hex(12)}"
    clipboard.copy(sentinel)
    _send_select_all_copy(hwnd, settle_seconds)
    time.sleep(0.2)
    copied = str(clipboard.paste())
    if copied == sentinel:
        return None
    _send_window_key(hwnd, _VK_RIGHT)  # Collapse selection without changing text.
    return copied


_OCR_ENGINE: Any = None


def _ocr_window_region(
    hwnd: int,
    left_ratio: float,
    top_ratio: float,
    width_ratio: float,
    height_ratio: float,
    min_confidence: float,
    max_text_top_px: int | None = None,
) -> list[str]:
    global _OCR_ENGINE
    try:
        import numpy as np
        import pyautogui
        from rapidocr_onnxruntime import RapidOCR
        # RapidOCR 1.x keeps legacy module names in config.yaml
        # (``ch_ppocr_v3_det`` etc.). In a PyInstaller bundle those names are
        # not automatically registered as top-level modules, which makes the
        # dynamic import resolve to an incomplete namespace. Bind the actual
        # packaged modules explicitly before constructing the engine.
        import sys
        import rapidocr_onnxruntime.ch_ppocr_v2_cls as cls_module
        import rapidocr_onnxruntime.ch_ppocr_v3_det as det_module
        import rapidocr_onnxruntime.ch_ppocr_v3_rec as rec_module

        sys.modules["ch_ppocr_v2_cls"] = cls_module
        sys.modules["ch_ppocr_v3_det"] = det_module
        sys.modules["ch_ppocr_v3_rec"] = rec_module
    except ImportError as exc:  # pragma: no cover - Windows-only dependencies
        raise WindowsUiError("安全发送需要 rapidocr-onnxruntime 以核对目标群") from exc
    foreground = _foreground_window()
    if foreground != hwnd:
        same_process = _belongs_to_same_process(foreground, hwnd)
        logger.info(
            "WeCom foreground changed during verification: target_hwnd=%s foreground_hwnd=%s same_process=%s",
            hwnd,
            foreground,
            same_process,
        )
        if not same_process:
            raise WindowsUiError("企微窗口已失去焦点，已终止发送")
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    region_left = left + int(width * left_ratio)
    region_top = top + int(height * top_ratio)
    region_width = max(120, int(width * width_ratio))
    region_height = max(45, int(height * height_ratio))
    screenshot = np.asarray(
        pyautogui.screenshot(region=(region_left, region_top, region_width, region_height))
    )
    result, _ = _OCR_ENGINE(screenshot)
    return [
        str(row[1]).strip()
        for row in (result or [])
        if len(row) >= 3
        and float(row[2]) >= min_confidence
        and (
            max_text_top_px is None
            or min(float(point[1]) for point in row[0]) <= max_text_top_px
        )
    ]


def _verify_search_query(hwnd: int, query: str, min_confidence: float) -> None:
    """Require the intended query to be visible before selecting a result."""
    expected = re.sub(r"\s+", "", query).lower()
    observed_texts: list[str] = []
    # Search text is rendered in a small custom control and can be missed by
    # OCR for one frame immediately after WM_CHAR input. Retry with a wider,
    # slightly more tolerant crop before declaring the destination invalid.
    for attempt in range(3):
        texts = _ocr_window_region(
            hwnd,
            0.075,
            0.002,
            0.19,
            0.12,
            max(0.45, min_confidence - 0.12),
        )
        observed_texts = texts
        observed = "".join(re.sub(r"\s+", "", text).lower() for text in texts)
        if expected and expected in observed:
            return
        if attempt < 2:
            time.sleep(0.18)
    raise WindowsUiError(
        f"群聊搜索校验失败：期望“{query}”，搜索框识别为“{' / '.join(observed_texts) or '空'}”"
    )


def _verify_group_header(hwnd: int, group_name: str, min_confidence: float) -> None:
    """OCR only the chat header and require the configured group name."""
    # The conversation begins around 21% of the maximized WeCom window. Start
    # slightly earlier so short group titles such as “测试群” keep enough OCR
    # margin, while still excluding the conversation list at the left.
    # RapidOCR needs some vertical context to detect the small title reliably,
    # but only text located in the first 70 px is accepted as header evidence.
    texts = _ocr_window_region(hwnd, 0.18, 0.002, 0.55, 0.18, min_confidence, max_text_top_px=70)
    expected = re.sub(r"\s+", "", group_name).lower()
    observed = "".join(re.sub(r"\s+", "", text).lower() for text in texts)
    if not expected or expected not in observed:
        raise WindowsUiError(f"目标群校验失败：期望“{group_name}”，窗口标题识别为“{' / '.join(texts) or '空'}”")


class WindowsWeComUiSender:
    """WeCom-targeted sender with target, editor and payload verification."""

    def __init__(self, config: WindowsUiConfig):
        self.config = config
        self._prepared: tuple[str, str] | None = None
        self._prepared_hwnd: int | None = None
        self._control_hwnd: int | None = None

    def is_ready(self) -> bool:
        return _visible_window_by_title(self.config.window_title) is not None

    def prepare_text(self, room_id: str, content: str) -> None:
        if not self.config.group_name:
            raise WindowsUiError("group_name is required for UI-based sending")
        hwnd = _visible_window_by_title(self.config.window_title)
        if hwnd is None:
            raise WindowsUiError("WeCom window is not visible; unlock and open WeCom first")
        # Use a deterministic layout. This removes the old dependency on a
        # particular window position, size, DPI scale, or remembered placement.
        clipboard = _clipboard()
        original_clipboard = clipboard.paste()
        try:
            self._control_hwnd = _minimize_control_window(self.config.control_window_title, hwnd)
            _activate_window(hwnd, self.config.settle_seconds)
            _focus_target_group(hwnd, self.config)
            _focus_editor(hwnd, self.config)

            existing_draft = _read_focused_editor_text(hwnd, self.config.settle_seconds)
            if existing_draft is not None and _normalized_text(existing_draft):
                if _normalized_text(existing_draft) != _normalized_text(content):
                    raise WindowsUiError("目标群输入框中已有其他未发送草稿，已保留草稿并终止自动发送")
                logger.info("reusing exact existing WeCom draft for room=%s", room_id)
            else:
                _send_window_key(hwnd, _VK_BACK)
                _paste_window_text(hwnd, content, self.config.settle_seconds)
            time.sleep(0.2)

            # A stale clipboard value could make a failed Ctrl+C look valid.
            # Replace it with a random sentinel first, then require an exact
            # copy-back from the focused editor before Enter is ever allowed.
            copied = _read_focused_editor_text(hwnd, self.config.settle_seconds)
            if copied is None or _normalized_text(copied) != _normalized_text(content):
                raise WindowsUiError("输入框内容回读失败，可能点到了工具栏或弹窗，已禁止发送")
            self._prepared = (room_id, content)
            self._prepared_hwnd = hwnd
        except Exception:
            _restore_control_window(self._control_hwnd)
            self._control_hwnd = None
            raise
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
            _focus_editor(hwnd, self.config)
            copied = _read_focused_editor_text(hwnd, self.config.settle_seconds)
            if copied is None or _normalized_text(copied) != _normalized_text(content):
                raise WindowsUiError("发送前输入框内容校验失败，已禁止发送")
            _activate_window(hwnd, 0.15)
            if _foreground_window() != hwnd:
                raise WindowsUiError("企微窗口再次失去焦点，已禁止发送")
            _verify_group_header(hwnd, self.config.group_name, self.config.ocr_min_confidence)
            # The final Enter must be a real keyboard event: WeCom's custom
            # editor can ignore a plain WM_KEYDOWN even when search accepts it.
            # The helper reasserts WeCom immediately before injecting this
            # single non-text key, so it cannot type into another application.
            _send_input_key_events(hwnd, [(_VK_RETURN, False), (_VK_RETURN, True)], 0.15)
            self._prepared = None
            self._prepared_hwnd = None
        finally:
            clipboard.copy(original_clipboard)
            _restore_control_window(self._control_hwnd)
            self._control_hwnd = None
