from __future__ import annotations

import re

from ..config import Settings
from ..db import Database
from ..models import RawMessage


def mentions_target(content: str, target_names: tuple[str, ...]) -> bool:
    normalized = content.replace("\u2005", " ").lower()
    return any(f"@{name.lower()}" in normalized for name in target_names)


class IngestionService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def should_capture(self, message: RawMessage) -> bool:
        if message.room_id != self.settings.target_room_id:
            return False
        if message.account_id == self.settings.target_account_id:
            return False
        return message.mentioned_account or mentions_target(message.content, self.settings.target_account_names)

    def ingest(self, message: RawMessage) -> bool:
        if not self.should_capture(message):
            return False
        return self.database.insert_message(message)


def strip_mention(content: str, target_names: tuple[str, ...]) -> str:
    result = content
    for name in target_names:
        result = re.sub(rf"@{re.escape(name)}", "", result, flags=re.IGNORECASE)
    return " ".join(result.split())
