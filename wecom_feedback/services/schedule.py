from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..config import Settings


def _parse_time(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(value)
    return time(hour, minute)


def schedule_slots(settings: Settings, day: date, tzinfo) -> list[datetime]:
    slots: list[datetime] = []
    if settings.summary_schedule_mode == "interval":
        try:
            start = datetime.combine(day, _parse_time(settings.summary_active_start), tzinfo=tzinfo)
            end = datetime.combine(day, _parse_time(settings.summary_active_end), tzinfo=tzinfo)
        except ValueError:
            return []
        if end < start:
            end += timedelta(days=1)
        cursor = start
        interval = timedelta(hours=max(1, settings.summary_interval_hours))
        while cursor <= end:
            slots.append(cursor)
            cursor += interval
        return slots
    for value in settings.summary_times:
        try:
            slots.append(datetime.combine(day, _parse_time(value), tzinfo=tzinfo))
        except ValueError:
            continue
    return sorted(slots)


def next_schedule_at(settings: Settings, now: datetime | None = None) -> datetime | None:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    candidates: list[datetime] = []
    for offset in (0, 1, 2):
        candidates.extend(schedule_slots(settings, current.date() + timedelta(days=offset), current.tzinfo))
    return min((slot for slot in candidates if slot > current), default=None)


def due_schedule_at(settings: Settings, now: datetime | None = None) -> datetime | None:
    """Return the current minute's slot; do not backfill old intervals after restart."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    for slot in schedule_slots(settings, current.date(), current.tzinfo):
        if slot.replace(second=0, microsecond=0) == current.replace(second=0, microsecond=0):
            return slot
    return None
