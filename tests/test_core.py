import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from wecom_feedback.config import Settings
from wecom_feedback.db import Database
from wecom_feedback.models import RawMessage
from wecom_feedback.services.feedback import FeedbackService
from wecom_feedback.services.ingestion import IngestionService, mentions_target, strip_mention
from wecom_feedback.services.workflow import WorkflowService
from wecom_feedback.adapters.bot import DryRunBot
from wecom_feedback.runtime import CollectorRuntime
from wecom_feedback.adapters.windows_ui_receiver import WindowsWeComUiReceiver
from wecom_feedback.models import SendJob
from wecom_feedback.adapters.sender import DeliveryUnconfirmed


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        self.db.init_schema()
        self.settings = Settings(
            database_path=Path(self.temp_dir.name) / "test.db",
            archive_enabled=False,
            archive_corp_id="",
            archive_secret="",
            archive_private_key_path="",
            target_room_id="room-1",
            target_group_name="客户群",
            target_group_remark="",
            target_account_id="agent-1",
            target_account_names=("系统反馈助手",),
            context_window_seconds=90,
            summary_times=("12:00",),
            poll_interval_seconds=10,
            dry_run=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mentions_target(self):
        self.assertTrue(mentions_target("请看 @系统反馈助手 这个问题", self.settings.target_account_names))
        self.assertFalse(mentions_target("普通消息", self.settings.target_account_names))

    def test_strip_mention_removes_duplicate_leading_display_name(self):
        self.assertEqual(
            strip_mention("@系统反馈助手 系统反馈助手：登录失败", self.settings.target_account_names),
            "登录失败",
        )

    def test_ingest_is_idempotent_and_creates_feedback(self):
        message = RawMessage(
            message_id="m-1",
            seq=1,
            account_id="customer-1",
            room_id="room-1",
            group_name="客户群",
            group_remark="",
            sender_id="customer-1",
            sender_name="客户A",
            message_type="text",
            raw_content="@系统反馈助手 登录后无法看到订单",
            content="@系统反馈助手 登录后无法看到订单",
            mentioned_account=True,
            created_at=datetime.now(timezone.utc),
        )
        ingestion = IngestionService(self.settings, self.db)
        self.assertTrue(ingestion.ingest(message))
        self.assertFalse(ingestion.ingest(message))
        item = FeedbackService(self.settings, self.db).create_from_message(message)
        self.assertEqual(item.feedback_type, "使用问题")
        self.assertEqual(self.db.counts()["raw_messages"], 1)
        self.assertEqual(self.db.counts()["feedback_items"], 1)

    def test_name_only_test_mode_can_capture_target_group(self):
        settings = self.settings.__class__(
            **{**self.settings.__dict__, "target_room_id": "", "target_account_id": "", "target_group_name": "测试群"}
        )
        message = RawMessage(
            message_id="name-only-1", seq=1, account_id="customer-4", room_id="unknown",
            group_name="测试群", group_remark="", sender_id="customer-4", sender_name="客户D",
            message_type="text", raw_content="@系统反馈助手 有个问题", content="@系统反馈助手 有个问题",
            mentioned_account=True,
        )
        self.assertTrue(IngestionService(settings, self.db).ingest(message))

    def test_summary_job_can_be_dispatched_in_dry_run(self):
        message = RawMessage(
            message_id="m-2",
            seq=2,
            account_id="customer-2",
            room_id="room-1",
            group_name="客户群",
            group_remark="",
            sender_id="customer-2",
            sender_name="客户B",
            message_type="text",
            raw_content="@系统反馈助手 希望增加导出功能",
            content="@系统反馈助手 希望增加导出功能",
            mentioned_account=True,
        )
        workflow = WorkflowService(self.settings, self.db, DryRunBot())
        self.assertTrue(workflow.process_message(message))
        job = workflow.schedule_summary()
        self.assertEqual(job.room_id, "room-1")
        self.assertEqual(self.db.counts()["send_jobs"], 1)

    def test_runtime_pulls_archive_and_advances_cursor(self):
        class FakeArchive:
            def pull_messages(self, cursor, limit=100):
                self.cursor = cursor
                return [RawMessage(
                    message_id="archive-1", seq=5, account_id="customer-3", room_id="room-1",
                    group_name="客户群", group_remark="", sender_id="customer-3", sender_name="客户C",
                    message_type="text", raw_content="@系统反馈助手 有一个报错",
                    content="@系统反馈助手 有一个报错", mentioned_account=True,
                )]

        class FakeSender:
            def is_ready(self):
                return True

            def send_text(self, room_id, content):
                self.last = (room_id, content)

        archive = FakeArchive()
        sender = FakeSender()
        settings = self.settings.__class__(
            **{**self.settings.__dict__, "archive_enabled": True, "archive_corp_id": "c",
               "archive_secret": "s", "archive_private_key_path": "k", "summary_times": ("23:59",)}
        )
        runtime = CollectorRuntime(settings, self.db, archive, DryRunBot(), sender)
        result = runtime.run_once()
        self.assertEqual(result["pulled"], 1)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(self.db.get_state("archive_cursor"), "5")

    def test_failed_send_job_is_delayed_before_retry(self):
        job = SendJob(
            job_id="retry-1",
            room_id="room-1",
            content="摘要",
            scheduled_at=datetime.now(timezone.utc),
        )
        self.db.create_send_job(job)
        self.assertEqual(len(self.db.claim_due_jobs()), 1)
        self.db.finish_job(job.job_id, success=False, error="企微暂不可用")
        self.assertEqual(self.db.claim_due_jobs(), [])
        stored = self.db.list_jobs()[0]
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["retry_count"], 1)

    def test_unconfirmed_send_is_not_automatically_retried(self):
        class UnconfirmedSender:
            def is_ready(self):
                return True

            def send_text(self, room_id, content):
                raise DeliveryUnconfirmed("无法确认")

        job = SendJob(
            job_id="uncertain-1", room_id="room-1", content="摘要",
            scheduled_at=datetime.now(timezone.utc),
        )
        self.db.create_send_job(job)
        workflow = WorkflowService(self.settings, self.db, DryRunBot())
        self.assertEqual(workflow.dispatch_due_jobs(UnconfirmedSender()), 0)
        self.assertEqual(self.db.get_job(job.job_id).status, "unconfirmed")
        self.assertEqual(self.db.claim_due_jobs(), [])
        self.assertTrue(self.db.retry_job(job.job_id))
        self.assertEqual(self.db.get_job(job.job_id).status, "pending")

    def test_feedback_can_be_edited_and_sync_status_reported(self):
        message = RawMessage(
            message_id="edit-1", seq=9, account_id="customer", room_id="room-1",
            group_name="客户群", group_remark="", sender_id="customer", sender_name="客户E",
            message_type="text", raw_content="@系统反馈助手 原始问题",
            content="@系统反馈助手 原始问题", mentioned_account=True,
        )
        workflow = WorkflowService(self.settings, self.db, DryRunBot())
        self.assertTrue(workflow.process_message(message))
        item = self.db.feedback_for_message("edit-1")
        updated = self.db.update_feedback(item.feedback_id, {"title": "修改后的问题", "priority": "P1"})
        self.assertEqual(updated.title, "修改后的问题")
        self.assertEqual(updated.priority, "P1")
        self.db.set_state(f"smart_table_synced:{item.feedback_id}", "1")
        self.assertEqual(self.db.smart_table_sync_counts("room-1"), {"total": 1, "synced": 1, "pending": 0})

    def test_one_summary_job_can_be_cancelled_and_retried(self):
        job = SendJob(
            job_id="cancel-1", room_id="room-1", content="摘要",
            scheduled_at=datetime.now(timezone.utc),
        )
        self.db.create_send_job(job)
        self.assertTrue(self.db.cancel_job(job.job_id))
        self.assertEqual(self.db.get_job(job.job_id).status, "cancelled")
        self.assertTrue(self.db.retry_job(job.job_id))
        self.assertEqual(self.db.get_job(job.job_id).status, "pending")

    def test_ui_receiver_filters_and_deduplicates_mentions(self):
        received = []
        receiver = WindowsWeComUiReceiver(self.settings, received.append)
        with patch("wecom_feedback.adapters.windows_ui_receiver._desktop_window", return_value=object()), patch(
            "wecom_feedback.adapters.windows_ui_receiver._visible_texts",
            return_value=["客户A", "@系统反馈助手 订单页面打不开", "@系统反馈助手 订单页面打不开"],
        ):
            self.assertEqual(receiver.poll_once(), 1)
            self.assertEqual(receiver.poll_once(), 0)
        self.assertEqual(received[0].group_name, "客户群")


if __name__ == "__main__":
    unittest.main()
