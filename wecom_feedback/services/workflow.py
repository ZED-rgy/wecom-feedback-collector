from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1

from ..adapters.bot import WeComBotAdapter
from ..adapters.sender import WeComAccountSender
from ..config import Settings
from ..db import Database
from ..models import RawMessage, SendJob
from .feedback import FeedbackService
from .ingestion import IngestionService


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
        items = (
            self.database.feedback_by_ids(feedback_ids)
            if feedback_ids is not None
            else self.database.list_feedback(room_key)
        )
        items = [item for item in items if item.status not in {"已忽略", "已完成"}]
        rendered_content = content.strip() if content and content.strip() else self.bot.render_summary(items)
        digest = sha1(f"{room_key}:{scheduled_at.isoformat()}".encode()).hexdigest()[:12]
        job = SendJob(
            job_id=f"SUMMARY-{scheduled_at.strftime('%Y%m%d%H%M')}-{digest}",
            room_id=room_key,
            content=rendered_content,
            scheduled_at=scheduled_at,
        )
        self.database.create_send_job(job)
        return job

    def dispatch_job(self, job_id: str, sender: WeComAccountSender) -> bool:
        if not sender.is_ready():
            return False
        job = self.database.claim_job(job_id)
        if job is None:
            return False
        try:
            sender.send_text(job.room_id, job.content)
        except Exception as exc:
            self.database.finish_job(job.job_id, success=False, error=str(exc))
            raise
        self.database.finish_job(job.job_id, success=True)
        return True

    def dispatch_due_jobs(self, sender: WeComAccountSender, limit: int = 10) -> int:
        if not sender.is_ready():
            return 0
        sent = 0
        for job in self.database.claim_due_jobs(limit):
            try:
                sender.send_text(job.room_id, job.content)
            except Exception as exc:
                self.database.finish_job(job.job_id, success=False, error=str(exc))
            else:
                self.database.finish_job(job.job_id, success=True)
                sent += 1
        return sent
