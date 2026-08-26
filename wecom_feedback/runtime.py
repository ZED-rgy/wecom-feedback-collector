from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .adapters.archive import ConversationArchiveAdapter
from .adapters.bot import WeComBotAdapter, build_bot
from .adapters.sender import WeComAccountSender
from .config import Settings
from .db import Database
from .models import RawMessage
from .services.workflow import WorkflowService
from .services.schedule import due_schedule_at

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

        scheduled = self._schedule_due_summaries() if self.settings.auto_send_enabled else 0
        sent = self.workflow.dispatch_due_jobs(self.sender) if self.settings.auto_send_enabled else 0
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
        if not (self.settings.target_room_id or self.settings.target_group_name):
            return 0
        now_local = datetime.now().astimezone()
        slot = due_schedule_at(self.settings, now_local)
        if slot is None:
            return 0
        room_key = self.settings.target_room_id or self.settings.target_group_name
        key = f"summary:{room_key}:{slot.strftime('%Y-%m-%d:%H:%M')}"
        if self.database.get_state(key):
            return 0
        self.workflow.schedule_summary(slot.astimezone(timezone.utc))
        self.database.set_state(key, "scheduled")
        return 1


def build_default_runtime(settings: Settings, database: Database) -> CollectorRuntime:
    from .adapters.archive import NotConfiguredArchive
    from .adapters.sender import DryRunSender

    return CollectorRuntime(settings, database, NotConfiguredArchive(), build_bot(settings), DryRunSender())
