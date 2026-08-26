from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

from ..models import FeedbackItem
from ..services.summary import render_feedback_summary
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
        return render_feedback_summary(items)


def build_bot(settings: "Settings") -> WeComBotAdapter:
    """Select the configured smart-table bot, or a local no-op fallback."""
    if settings.table_integration_enabled:
        return CliSmartTableBot(settings)
    return DryRunBot()
