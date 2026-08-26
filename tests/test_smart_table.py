import unittest
from datetime import datetime, timezone
from pathlib import Path

from wecom_feedback.adapters.smart_table import CliSmartTableBot, _date_value
from wecom_feedback.config import Settings
from wecom_feedback.models import FeedbackItem


class FakeTableBot(CliSmartTableBot):
    def __init__(self, settings, existing=None):
        super().__init__(settings)
        self.existing = existing or []
        self.calls = []

    def _run(self, command, payload):
        self.calls.append((command, payload))
        if command == ("fields", "list"):
            return {"fields": [{"field_title": name} for name in ("任务编号", "状态", "优先级")]}
        if command == ("records", "list"):
            return {"records": self.existing}
        return {"errcode": 0}


class SmartTableTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            database_path=Path("unused.db"), archive_enabled=False, archive_corp_id="", archive_secret="",
            archive_private_key_path="", target_room_id="", target_group_name="测试群", target_group_remark="",
            target_account_id="", target_account_names=("助手",), context_window_seconds=90,
            summary_times=("12:00",), poll_interval_seconds=10, dry_run=False,
            table_integration_enabled=True, smart_table_url="https://example.invalid/table",
        )
        self.item = FeedbackItem(
            feedback_id="FB-1", room_id="测试群", account_id="a", submitter="用户", feedback_type="使用问题",
            title="订单打不开", description="订单打不开", priority="P1", status="处理中",
            source_message_ids=("local-1",), confidence=1, need_more_info=False,
            created_at=datetime(2026, 8, 26, 15, 32, 45, tzinfo=timezone.utc),
        )

    def test_date_keeps_time(self):
        self.assertRegex(_date_value(self.item.created_at), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_existing_source_message_is_updated(self):
        bot = FakeTableBot(self.settings, [{
            "record_id": "row-1", "values": {"来源消息ID": [{"text": "local-1", "type": "text"}]}
        }])
        bot.upsert_feedback(self.item)
        command, payload = bot.calls[-1]
        self.assertEqual(command, ("records", "update"))
        self.assertEqual(payload["records"][0]["record_id"], "row-1")
        self.assertIn("任务编号", payload["records"][0]["values"])

    def test_records_are_read_page_by_page(self):
        class PagedBot(FakeTableBot):
            def __init__(self, settings):
                super().__init__(settings)
                self.page = 0

            def _run(self, command, payload):
                self.calls.append((command, payload))
                if command == ("records", "list"):
                    self.page += 1
                    if self.page == 1:
                        return {"records": [{"record_id": "row-1", "values": {}}], "has_more": True}
                    return {"records": [{"record_id": "row-2", "values": {}}], "has_more": False}
                return super()._run(command, payload)

        bot = PagedBot(self.settings)
        records = bot.list_records()
        self.assertEqual([record["record_id"] for record in records], ["row-1", "row-2"])
        self.assertEqual(sum(command == ("records", "list") for command, _ in bot.calls), 2)


if __name__ == "__main__":
    unittest.main()
