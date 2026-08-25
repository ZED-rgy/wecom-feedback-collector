from __future__ import annotations

from typing import Protocol


class WeComAccountSender(Protocol):
    def is_ready(self) -> bool:
        ...

    def send_text(self, room_id: str, content: str) -> None:
        ...


class DryRunSender:
    def is_ready(self) -> bool:
        return True

    def send_text(self, room_id: str, content: str) -> None:
        print(f"[dry-run] send to room={room_id}:\n{content}")
