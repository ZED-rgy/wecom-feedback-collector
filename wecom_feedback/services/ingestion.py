from __future__ import annotations

import re

from ..config import Settings
from ..db import Database
from ..models import RawMessage


def mentions_target(content: str, target_names: tuple[str, ...]) -> bool:
    normalized = content.replace("\u2005", " ").lower()
    if any(f"@{name.lower()}" in normalized for name in target_names):
        return True
    # WeCom's local message database may store a mention as a leading
    # display-name line (without the @ glyph), especially for messages sent
    # by a newly-added external contact. Require a newline and non-empty body
    # so an ordinary sentence that merely starts with the account name is not
    # treated as a mention.
    lines = normalized.splitlines()
    if len(lines) < 2 or not lines[0].strip():
        return False
    first_line = lines[0].strip()
    return any(
        first_line == name.lower() and any(line.strip() for line in lines[1:])
        for name in target_names
    )


class IngestionService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def should_capture(self, message: RawMessage) -> bool:
        target_room = self.settings.target_room_id
        same_room = message.room_id == target_room if target_room else (
            bool(self.settings.target_group_name)
            and message.group_name == self.settings.target_group_name
        )
        if not same_room:
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
        # The local protobuf payload may expose the mentioned display name a
        # second time without the @ glyph. Remove it only when it is a leading
        # mention token, not when the name appears naturally later in the text.
        result = re.sub(
            rf"^\s*{re.escape(name)}(?:(?:\s*[:：,，]\s*)|\s+)",
            "",
            result,
            count=1,
            flags=re.IGNORECASE,
        )
    return " ".join(result.split())
