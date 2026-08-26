import unittest
from unittest.mock import MagicMock, call, patch

from wecom_feedback.adapters.windows_ui import (
    ConfirmationRequired,
    WindowsUiConfig,
    WindowsWeComUiSender,
    _belongs_to_same_process,
    _normalized_text,
    _paste_window_text,
)


class WindowsUiSenderTests(unittest.TestCase):
    def test_send_requires_explicit_confirmation(self):
        sender = WindowsWeComUiSender(WindowsUiConfig(group_name="测试群"))
        with self.assertRaises(ConfirmationRequired):
            sender.send_text("测试群", "摘要")

    @patch("wecom_feedback.adapters.windows_ui._visible_window_by_title", return_value=None)
    def test_readiness_checks_visible_window(self, _window):
        sender = WindowsWeComUiSender(WindowsUiConfig(group_name="测试群"))
        self.assertFalse(sender.is_ready())

    def test_clipboard_comparison_normalizes_line_endings(self):
        self.assertEqual(_normalized_text("第一行\r\n第二行  "), "第一行\n第二行")

    @patch("wecom_feedback.adapters.windows_ui._send_input_key_events")
    @patch("wecom_feedback.adapters.windows_ui._clipboard")
    def test_message_paste_targets_verified_wecom_window(self, clipboard_factory, send_events):
        clipboard = MagicMock()
        clipboard_factory.return_value = clipboard

        _paste_window_text(101, "摘要内容", 0.4)

        clipboard.copy.assert_called_once_with("摘要内容")
        send_events.assert_called_once_with(
            101,
            [(0x11, False), (0x56, False), (0x56, True), (0x11, True)],
            0.4,
        )

    @patch("wecom_feedback.adapters.windows_ui._window_process_id", side_effect=[9001, 9001])
    def test_auxiliary_wecom_window_is_accepted_as_same_process(self, _process_id):
        self.assertTrue(_belongs_to_same_process(202, 101))

    @patch("wecom_feedback.adapters.windows_ui._window_process_id", side_effect=[7001, 9001])
    def test_unrelated_foreground_window_is_rejected(self, _process_id):
        self.assertFalse(_belongs_to_same_process(202, 101))

    @patch("wecom_feedback.adapters.windows_ui._verify_group_header")
    @patch("wecom_feedback.adapters.windows_ui._send_input_key_events")
    @patch("wecom_feedback.adapters.windows_ui._restore_control_window")
    @patch("wecom_feedback.adapters.windows_ui._foreground_window", return_value=101)
    @patch("wecom_feedback.adapters.windows_ui._read_focused_editor_text", return_value="发送内容")
    @patch("wecom_feedback.adapters.windows_ui._focus_editor")
    @patch("wecom_feedback.adapters.windows_ui._focus_target_group")
    @patch("wecom_feedback.adapters.windows_ui._activate_window")
    @patch("wecom_feedback.adapters.windows_ui._visible_window_by_title", return_value=101)
    @patch("wecom_feedback.adapters.windows_ui._clipboard")
    def test_confirm_reacquires_group_and_revalidates_payload_before_enter(
        self,
        clipboard_factory,
        _visible,
        activate,
        focus_group,
        focus_editor,
        read_editor,
        _foreground,
        restore_control,
        send_input_events,
        verify_header,
    ):
        clipboard = MagicMock()
        clipboard.paste.return_value = "原剪贴板"
        clipboard_factory.return_value = clipboard
        sender = WindowsWeComUiSender(WindowsUiConfig(group_name="测试群"))
        sender._prepared = ("测试群", "发送内容")
        sender._prepared_hwnd = 101
        sender._control_hwnd = 202

        sender.confirm_and_send("测试群", "发送内容", confirmed=True)

        self.assertEqual(activate.call_count, 2)
        focus_group.assert_called_once_with(101, sender.config)
        focus_editor.assert_called_once_with(101, sender.config)
        read_editor.assert_called_once_with(101, sender.config.settle_seconds)
        verify_header.assert_called_once_with(101, "测试群", sender.config.ocr_min_confidence)
        send_input_events.assert_called_once_with(101, [(0x0D, False), (0x0D, True)], 0.15)
        clipboard.copy.assert_has_calls([call("原剪贴板")])
        restore_control.assert_called_once_with(202)
        self.assertIsNone(sender._prepared)
        self.assertIsNone(sender._control_hwnd)

    @patch("wecom_feedback.adapters.windows_ui._read_focused_editor_text", return_value="被替换的内容")
    @patch("wecom_feedback.adapters.windows_ui._restore_control_window")
    @patch("wecom_feedback.adapters.windows_ui._focus_editor")
    @patch("wecom_feedback.adapters.windows_ui._focus_target_group")
    @patch("wecom_feedback.adapters.windows_ui._activate_window")
    @patch("wecom_feedback.adapters.windows_ui._visible_window_by_title", return_value=101)
    @patch("wecom_feedback.adapters.windows_ui._clipboard")
    @patch("wecom_feedback.adapters.windows_ui._send_input_key_events")
    def test_confirm_never_presses_enter_when_editor_content_changed(
        self,
        send_input_events,
        clipboard_factory,
        _visible,
        _activate,
        _focus_group,
        _focus_editor,
        restore_control,
        _read_editor,
    ):
        clipboard = MagicMock()
        clipboard.paste.return_value = "原剪贴板"
        clipboard_factory.return_value = clipboard
        sender = WindowsWeComUiSender(WindowsUiConfig(group_name="测试群"))
        sender._prepared = ("测试群", "发送内容")
        sender._prepared_hwnd = 101
        sender._control_hwnd = 202

        with self.assertRaisesRegex(Exception, "发送前输入框内容校验失败"):
            sender.confirm_and_send("测试群", "发送内容", confirmed=True)

        send_input_events.assert_not_called()
        clipboard.copy.assert_called_once_with("原剪贴板")
        restore_control.assert_called_once_with(202)


if __name__ == "__main__":
    unittest.main()
