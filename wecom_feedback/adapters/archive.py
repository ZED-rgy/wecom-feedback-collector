from __future__ import annotations

from typing import Protocol

from ..models import RawMessage


class ConversationArchiveAdapter(Protocol):
    def pull_messages(self, cursor: int, limit: int = 100) -> list[RawMessage]:
        """Return messages after the last cursor/sequence."""


class NotConfiguredArchive:
    def pull_messages(self, cursor: int, limit: int = 100) -> list[RawMessage]:
        raise RuntimeError("conversation archive adapter is not configured")
