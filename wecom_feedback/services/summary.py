from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

from ..models import FeedbackItem


def render_feedback_summary(items: Sequence[FeedbackItem]) -> str:
    if not items:
        return "本时段暂无新的需求或问题。"

    # When the local-store receiver is active its stable message IDs and sender
    # identities are more reliable than earlier OCR observations of the same
    # chat. Keep OCR as a fallback, but do not mix both sources in one summary.
    local_items = [
        item
        for item in items
        if any(message_id.startswith("local-") for message_id in item.source_message_ids)
    ]
    selected = local_items or list(items)

    grouped: OrderedDict[tuple[str, str, str], tuple[FeedbackItem, int]] = OrderedDict()
    for item in selected:
        key = (item.priority, item.title.strip(), item.status)
        existing = grouped.get(key)
        grouped[key] = (item, existing[1] + 1 if existing else 1)

    lines = ["【客户群需求/问题摘要】"]
    if len(selected) != len(grouped):
        lines.append(f"共收集 {len(selected)} 条反馈，合并为 {len(grouped)} 项：")
    for index, (item, count) in enumerate(grouped.values(), start=1):
        count_text = f"，出现{count}次" if count > 1 else ""
        lines.append(f"{index}. [{item.priority}] {item.title}（{item.status}{count_text}）")
    return "\n".join(lines)
