from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha1

from ..adapters.bot import WeComBotAdapter
from ..adapters.sender import DeliveryUnconfirmed, WeComAccountSender
from ..config import Settings
from ..db import Database
from ..models import RawMessage, SendJob
from .feedback import FeedbackService
from .ingestion import IngestionService
from .reporting import build_report, mark_report_sent


logger = logging.getLogger("wecom_feedback.workflow")


class WorkflowService:
    """Orchestrate adapters while keeping the business flow testable."""

    def __init__(self, settings: Settings, database: Database, bot: WeComBotAdapter):
        self.settings = settings
        self.database = database
        self.bot = bot
        self.ingestion = IngestionService(settings, database)
        self.feedback = FeedbackService(settings, database)

    def process_message(self, message: RawMessage) -> bool:
        if not self.ingestion.ingest(message):
            # UI polling has no stable message ID. If the message was already
            # ingested, use it as an opportunity to retry a failed table sync.
            item = self.database.feedback_for_message(message.message_id)
            if item is None:
                return False
        else:
            item = self.feedback.create_from_message(message)
            if item is None:
                item = self.database.feedback_for_message(message.message_id)
        if item is None:
            return True
        sync_key = f"smart_table_synced:{item.feedback_id}"
        should_sync = self.settings.table_integration_enabled and not self.settings.dry_run
        if should_sync and not self.database.get_state(sync_key):
            self.bot.upsert_feedback(item)
            self.database.set_state(sync_key, "1")
        elif not self.settings.table_integration_enabled:
            self.bot.upsert_feedback(item)
        return True

    def schedule_summary(
        self,
        scheduled_at: datetime | None = None,
        feedback_ids: list[str] | None = None,
        content: str | None = None,
    ) -> SendJob:
        scheduled_at = scheduled_at or datetime.now(timezone.utc)
        room_key = self.settings.target_room_id or self.settings.target_group_name
        if content and content.strip():
            rendered_content = content.strip()
        elif feedback_ids is not None:
            items = self.database.feedback_by_ids(feedback_ids)
            items = [item for item in items if item.status not in {"已忽略", "已完成"}]
            rendered_content = self.bot.render_summary(items)
        else:
            rendered_content = str(build_report(self.settings, self.database, self.bot)["content"])
        digest = sha1(f"{room_key}:{scheduled_at.isoformat()}".encode()).hexdigest()[:12]
        duplicate = self.database.find_active_job(
            room_key,
            self.settings.target_group_name,
            rendered_content,
            scheduled_at - timedelta(minutes=10),
        )
        if duplicate is not None:
            return duplicate
        job = SendJob(
            job_id=f"SUMMARY-{scheduled_at.strftime('%Y%m%d%H%M')}-{digest}",
            room_id=room_key,
            content=rendered_content,
            scheduled_at=scheduled_at,
            target_group_name=self.settings.target_group_name,
        )
        self.database.create_send_job(job)
        return job

    def dispatch_job(self, job_id: str, sender: WeComAccountSender) -> bool:
        if not sender.is_ready():
            return False
        job = self.database.claim_job(job_id)
        if job is None:
            return False
        return self.dispatch_claimed_job(job, sender)

    def dispatch_claimed_job(self, job: SendJob, sender: WeComAccountSender) -> bool:
        """Dispatch a job already reserved by a background send worker."""
        expected_room = self.settings.target_room_id or self.settings.target_group_name
        if job.room_id != expected_room or job.target_group_name != self.settings.target_group_name:
            reason = "任务所属群已变更，已取消发送"
            self.database.cancel_claimed_job(job.job_id, reason)
            logger.warning("skip job %s after target group changed", job.job_id)
            return False
        if not sender.is_ready():
            self.database.finish_job(job.job_id, success=False, error="企微主窗口未打开，任务已保留")
            return False
        try:
            sender.send_text(job.room_id, job.content)
        except DeliveryUnconfirmed as exc:
            self.database.mark_job_unconfirmed(job.job_id, str(exc))
            return False
        except Exception as exc:
            self.database.finish_job(job.job_id, success=False, error=str(exc))
            raise
        self.database.finish_job(job.job_id, success=True)
        mark_report_sent(self.database, job.room_id)
        return True

    def dispatch_due_jobs(self, sender: WeComAccountSender, limit: int = 10) -> int:
        if not sender.is_ready():
            return 0
        sent = 0
        expected_room = self.settings.target_room_id or self.settings.target_group_name
        for job in self.database.claim_due_jobs(limit, room_id=expected_room, group_name=self.settings.target_group_name):
            try:
                sender.send_text(job.room_id, job.content)
            except DeliveryUnconfirmed as exc:
                self.database.mark_job_unconfirmed(job.job_id, str(exc))
            except Exception as exc:
                self.database.finish_job(job.job_id, success=False, error=str(exc))
            else:
                self.database.finish_job(job.job_id, success=True)
                mark_report_sent(self.database, job.room_id)
                sent += 1
        return sent
