from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .adapters.archive import ConversationArchiveAdapter
from .adapters.bot import WeComBotAdapter
from .adapters.sender import WeComAccountSender
from .config import Settings
from .db import Database
from .models import RawMessage
from .services.workflow import WorkflowService

logger = logging.getLogger("wecom_feedback.runtime")


class CollectorRuntime:
    """Long-lived orchestration loop; real adapters can be injected later."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        archive: ConversationArchiveAdapter,
        bot: WeComBotAdapter,
        sender: WeComAccountSender,
    ):
        self.settings = settings
        self.database = database
        self.archive = archive
        self.workflow = WorkflowService(settings, database, bot)
        self.sender = sender
        self.stop_event = threading.Event()

    def run_once(self) -> dict[str, int | str]:
        processed = 0
        pulled = 0
        cursor = int(self.database.get_state("archive_cursor", "0"))
        if self.settings.archive_enabled:
            messages = self.archive.pull_messages(cursor, limit=100)
            pulled = len(messages)
            for message in messages:
                if self.workflow.process_message(message):
                    processed += 1
                cursor = max(cursor, message.seq)
            if cursor != int(self.database.get_state("archive_cursor", "0")):
                self.database.set_state("archive_cursor", str(cursor))

        scheduled = self._schedule_due_summaries()
        sent = self.workflow.dispatch_due_jobs(self.sender)
        return {"pulled": pulled, "processed": processed, "scheduled": scheduled, "sent": sent}

    def run_forever(self, poll_interval: int | None = None) -> None:
        interval = max(2, poll_interval or self.settings.poll_interval_seconds)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logger.info("collector runtime started; interval=%ss archive=%s", interval, self.settings.archive_enabled)
        while not self.stop_event.is_set():
            try:
                logger.info("runtime cycle: %s", self.run_once())
            except Exception:
                logger.exception("runtime cycle failed; will retry")
            self.stop_event.wait(interval)

    def stop(self) -> None:
        self.stop_event.set()

    def _schedule_due_summaries(self) -> int:
        if not self.settings.target_room_id:
            return 0
        now_local = datetime.now().astimezone()
        scheduled = 0
        for time_text in self.settings.summary_times:
            try:
                hour, minute = (int(part) for part in time_text.split(":", 1))
            except ValueError:
                logger.warning("invalid summary time: %s", time_text)
                continue
            if (now_local.hour, now_local.minute) < (hour, minute):
                continue
            key = f"summary:{now_local.date().isoformat()}:{hour:02d}:{minute:02d}"
            if self.database.get_state(key):
                continue
            self.workflow.schedule_summary(now_local.astimezone(timezone.utc))
            self.database.set_state(key, "scheduled")
            scheduled += 1
        return scheduled


def build_default_runtime(settings: Settings, database: Database) -> CollectorRuntime:
    from .adapters.archive import NotConfiguredArchive
    from .adapters.bot import DryRunBot
    from .adapters.sender import DryRunSender

    return CollectorRuntime(settings, database, NotConfiguredArchive(), DryRunBot(), DryRunSender())
