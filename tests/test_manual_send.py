import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wecom_feedback.adapters.bot import DryRunBot
from wecom_feedback.adapters.sender import DryRunSender
from wecom_feedback.config import Settings
from wecom_feedback.db import Database
from wecom_feedback.models import SendJob
from wecom_feedback.webapp import ManualSendController


class ManualSendControllerTests(unittest.TestCase):
    def test_confirmed_job_runs_after_response_settle_delay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "test.db"
            database = Database(database_path)
            database.init_schema()
            settings = Settings(
                database_path=database_path,
                archive_enabled=False,
                archive_corp_id="",
                archive_secret="",
                archive_private_key_path="",
                target_room_id="room-1",
                target_group_name="测试群",
                target_group_remark="",
                target_account_id="agent-1",
                target_account_names=("反馈助手",),
                context_window_seconds=90,
                summary_times=("12:00",),
                poll_interval_seconds=10,
                dry_run=True,
            )
            job = SendJob(
                job_id="queued-manual-1",
                room_id="room-1",
                content="测试摘要",
                scheduled_at=datetime.now(timezone.utc),
            )
            database.create_send_job(job)
            claimed = database.claim_job(job.job_id)
            controller = ManualSendController(settle_seconds=0.01)
            with patch("wecom_feedback.webapp.build_bot", return_value=DryRunBot()), patch(
                "wecom_feedback.adapters.sender.build_manual_sender", return_value=DryRunSender()
            ):
                controller.enqueue(settings, claimed)
                deadline = time.monotonic() + 2
                while controller.status()["last_job_id"] != job.job_id and time.monotonic() < deadline:
                    time.sleep(0.02)
                controller.stop()

            self.assertEqual(database.get_job(job.job_id).status, "sent")
            self.assertEqual(controller.status()["last_error"], "")


if __name__ == "__main__":
    unittest.main()
