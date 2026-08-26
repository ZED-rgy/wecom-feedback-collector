import unittest
from unittest.mock import patch

from wecom_feedback.adapters.windows_ui import (
    ConfirmationRequired,
    WindowsUiConfig,
    WindowsWeComUiSender,
    _normalized_text,
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


if __name__ == "__main__":
    unittest.main()
