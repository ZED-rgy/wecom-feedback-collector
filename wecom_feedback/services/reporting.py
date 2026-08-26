from __future__ import annotations

from datetime import datetime, timedelta, timezone
from string import Formatter
from typing import Any

from ..config import Settings
from ..db import Database


class _SafeValues(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _task_lines(tasks: list[dict[str, object]], empty_text: str) -> str:
    if not tasks:
        return empty_text
    return "\n".join(
        f"- {task['task_id']} [{task['priority']}] {task['title']}（{task['status']}）"
        for task in tasks
    )


def _local_snapshot(
    settings: Settings, database: Database, since: datetime, limit: int
) -> dict[str, object]:
    room_key = settings.target_room_id or settings.target_group_name
    now = datetime.now().astimezone()
    today = now.date()
    tasks = [
        {
            "task_id": item.feedback_id,
            "title": item.title,
            "submitter": item.submitter,
            "status": item.status,
            "priority": item.priority,
            "created_at": item.created_at.astimezone(),
            "updated_at": item.updated_at.astimezone(),
        }
        for item in database.list_feedback(room_key, limit=1000)
        if item.status != "已忽略"
    ]
    recent = sorted(
        (task for task in tasks if task["created_at"] >= since.astimezone()),
        key=lambda task: task["created_at"],
        reverse=True,
    )[:limit]
    focus = sorted(
        (
            task for task in tasks
            if task["status"] != "已完成" and task["priority"] in {"P0", "P1"}
        ),
        key=lambda task: (task["priority"], task["created_at"]),
    )[:limit]
    return {
        "source": "local_fallback",
        "total": len(tasks),
        "today_new": sum(1 for task in tasks if task["created_at"].date() == today),
        "pending_confirmation": sum(1 for task in tasks if task["status"] == "待确认"),
        "in_progress": sum(1 for task in tasks if task["status"] in {"处理中", "进行中"}),
        "completed_today": sum(
            1 for task in tasks if task["status"] == "已完成" and task["updated_at"].date() == today
        ),
        "recent": recent,
        "focus": focus,
        "tasks": tasks,
    }


def build_report(
    settings: Settings,
    database: Database,
    bot: object,
    now: datetime | None = None,
) -> dict[str, object]:
    now = (now or datetime.now(timezone.utc)).astimezone()
    room_key = settings.target_room_id or settings.target_group_name
    last_sent_text = database.get_state(f"last_summary_sent_at:{room_key}")
    try:
        since = datetime.fromisoformat(last_sent_text).astimezone() if last_sent_text else None
    except ValueError:
        since = None
    since = since or (now - timedelta(hours=settings.summary_interval_hours))
    warning = ""
    try:
        if not settings.table_integration_enabled or not hasattr(bot, "reporting_snapshot"):
            raise RuntimeError("智能表格读取未启用")
        snapshot = bot.reporting_snapshot(since=since, limit=settings.summary_detail_limit)
    except Exception as exc:
        snapshot = _local_snapshot(settings, database, since, settings.summary_detail_limit)
        warning = str(exc)

    values: dict[str, object] = {
        "group_name": settings.target_group_name or room_key,
        "report_time": now.strftime("%Y-%m-%d %H:%M"),
        "interval_hours": settings.summary_interval_hours,
        "today_new": snapshot["today_new"],
        "pending_confirmation": snapshot["pending_confirmation"],
        "in_progress": snapshot["in_progress"],
        "completed_today": snapshot["completed_today"],
        "total": snapshot["total"],
        "recent_items": _task_lines(snapshot["recent"], "- 本时段暂无新增"),
        "focus_items": _task_lines(snapshot["focus"], "- 暂无高优先级待办"),
    }
    # Parse first so malformed braces produce a useful validation error.
    list(Formatter().parse(settings.summary_template))
    content = settings.summary_template.format_map(_SafeValues(values)).strip()
    return {
        **snapshot,
        "content": content,
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "warning": warning,
    }


def mark_report_sent(database: Database, room_key: str, sent_at: datetime | None = None) -> None:
    database.set_state(
        f"last_summary_sent_at:{room_key}",
        (sent_at or datetime.now(timezone.utc)).isoformat(),
    )
