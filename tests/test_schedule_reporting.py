import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from wecom_feedback.adapters.bot import DryRunBot
from wecom_feedback.config import Settings
from wecom_feedback.db import Database
from wecom_feedback.models import FeedbackItem
from wecom_feedback.services.reporting import build_report
from wecom_feedback.services.schedule import due_schedule_at, next_schedule_at, schedule_slots


def settings_for(path: Path, **changes: object) -> Settings:
    base = Settings(
        database_path=path,
        archive_enabled=False,
        archive_corp_id="",
        archive_secret="",
        archive_private_key_path="",
        target_room_id="room-1",
        target_group_name="测试群",
        target_group_remark="",
        target_account_id="agent",
        target_account_names=("助手",),
        context_window_seconds=90,
        summary_times=("12:00", "18:00"),
        poll_interval_seconds=10,
        dry_run=False,
        summary_schedule_mode="interval",
        summary_interval_hours=2,
        summary_active_start="08:00",
        summary_active_end="22:00",
    )
    return Settings(**{**base.__dict__, **changes})


class ScheduleReportingTests(unittest.TestCase):
    def test_two_hour_slots_and_no_catch_up(self):
        settings = settings_for(Path("unused.db"))
        slots = schedule_slots(settings, date(2026, 8, 26), timezone.utc)
        self.assertEqual([slot.strftime("%H:%M") for slot in slots], [
            "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"
        ])
        self.assertIsNone(due_schedule_at(settings, datetime(2026, 8, 26, 15, 59, tzinfo=timezone.utc)))
        self.assertEqual(
            due_schedule_at(settings, datetime(2026, 8, 26, 16, 0, 35, tzinfo=timezone.utc)).hour,
            16,
        )
        self.assertEqual(
            next_schedule_at(settings, datetime(2026, 8, 26, 16, 1, tzinfo=timezone.utc)).hour,
            18,
        )

    def test_report_template_uses_table_snapshot(self):
        class TableBot(DryRunBot):
            def reporting_snapshot(self, since, limit):
                return {
                    "source": "smart_table", "today_new": 2, "pending_confirmation": 3,
                    "in_progress": 1, "completed_today": 4, "total": 9,
                    "recent": [{"task_id": "FB-1", "priority": "P1", "title": "登录异常", "status": "待确认"}],
                    "focus": [], "tasks": [],
                }

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            db = Database(path)
            db.init_schema()
            settings = settings_for(path, table_integration_enabled=True)
            report = build_report(settings, db, TableBot(), datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc))
            self.assertEqual(report["source"], "smart_table")
            self.assertIn("今日新增：2 项", report["content"])
            self.assertIn("FB-1", report["content"])

    def test_report_falls_back_to_local_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            db = Database(path)
            db.init_schema()
            settings = settings_for(path, table_integration_enabled=True)
            now = datetime.now(timezone.utc)
            db.save_feedback(FeedbackItem(
                feedback_id="FB-LOCAL", room_id="room-1", account_id="a", submitter="用户",
                feedback_type="使用问题", title="订单打不开", description="订单打不开",
                priority="P2", status="待确认", source_message_ids=("m1",), confidence=1,
                need_more_info=False, created_at=now, updated_at=now,
            ))
            report = build_report(settings, db, DryRunBot())
            self.assertEqual(report["source"], "local_fallback")
            self.assertEqual(report["total"], 1)
            self.assertTrue(report["warning"])


if __name__ == "__main__":
    unittest.main()
