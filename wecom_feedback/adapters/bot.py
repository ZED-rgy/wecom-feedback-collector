from __future__ import annotations

from typing import Protocol, Sequence

from ..models import FeedbackItem


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
