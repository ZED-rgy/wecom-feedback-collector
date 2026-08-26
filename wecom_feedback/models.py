from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RawMessage:
    message_id: str
    seq: int
    account_id: str
    room_id: str
    group_name: str
    group_remark: str
    sender_id: str
    sender_name: str
    message_type: str
    raw_content: str
    content: str
    mentioned_account: bool
    created_at: datetime = field(default_factory=utc_now)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedbackItem:
    feedback_id: str
    room_id: str
    account_id: str
    submitter: str
    feedback_type: str
    title: str
    description: str
    priority: str
    status: str
    source_message_ids: tuple[str, ...]
    confidence: float
    need_more_info: bool
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class SendJob:
    job_id: str
    room_id: str
    content: str
    scheduled_at: datetime
    # Snapshot the target at scheduling time so a later group switch cannot
    # redirect an old job to the new group.
    target_group_name: str = ""
    status: str = "pending"
    retry_count: int = 0
    last_error: str = ""
