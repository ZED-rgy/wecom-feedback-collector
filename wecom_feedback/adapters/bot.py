from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

from ..models import FeedbackItem
from .smart_table import CliSmartTableBot

if TYPE_CHECKING:
    from ..config import Settings


class WeComBotAdapter(Protocol):
    def upsert_feedback(self, item: FeedbackItem) -> None:
        ...

    def render_summary(self, items: Sequence[FeedbackItem]) -> str:
        ...


class DryRunBot:
    def upsert_feedback(self, item: FeedbackItem) -> None:
        return None

    def render_summary(self, items: Sequence[FeedbackItem]) -> str:
        if not items:
            return "本时段暂无新的需求或问题。"
        lines = ["【客户群需求/问题摘要】"]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. [{item.priority}] {item.title}（{item.status}）")
        return "\n".join(lines)


def build_bot(settings: "Settings") -> WeComBotAdapter:
    """Select the configured smart-table bot, or a local no-op fallback."""
    if settings.table_integration_enabled:
        return CliSmartTableBot(settings)
    return DryRunBot()
