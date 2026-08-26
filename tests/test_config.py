import tempfile
import unittest
from pathlib import Path

from wecom_feedback.config import ConfigValidationError, save_env


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "target_group_name": "测试群",
            "target_group_remark": "测试群",
            "target_account_names": "冉光意",
            "context_window_seconds": 90,
            "poll_interval_seconds": 10,
            "summary_schedule_mode": "interval",
            "summary_interval_hours": 2,
            "summary_active_start": "08:00",
            "summary_active_end": "22:00",
            "summary_times": "12:00,18:00",
            "summary_template": "{group_name} {total}",
            "summary_detail_limit": 5,
        }

    def test_invalid_values_are_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaises(ConfigValidationError):
                save_env({**self.values, "poll_interval_seconds": "not-a-number"}, path)
            self.assertFalse(path.exists())

    def test_save_is_atomic_and_keeps_template(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            save_env(self.values, path)
            content = path.read_text(encoding="utf-8")
            self.assertIn("WECOM_SUMMARY_TEMPLATE_B64=", content)
            self.assertFalse((path.parent / ".env.tmp").exists())


if __name__ == "__main__":
    unittest.main()
