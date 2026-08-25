from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..adapters.ai import FeedbackExtractor, RuleBasedFeedbackExtractor
from ..config import Settings
from ..db import Database
from ..models import FeedbackItem, RawMessage
from .ingestion import strip_mention


class FeedbackService:
    def __init__(self, settings: Settings, database: Database, extractor: FeedbackExtractor | None = None):
        self.settings = settings
        self.database = database
        self.extractor = extractor or RuleBasedFeedbackExtractor()

    def create_from_message(self, message: RawMessage) -> FeedbackItem | None:
        if self.database.feedback_exists_for_message(message.message_id):
            return None
        description = strip_mention(message.content, self.settings.target_account_names).strip()
        description = description or message.content.strip()
        extracted = self.extractor.extract(description)
        title = description[:48] + ("…" if len(description) > 48 else "")
        digest = hashlib.sha1(message.message_id.encode("utf-8")).hexdigest()[:10]
        now = datetime.now(timezone.utc)
        item = FeedbackItem(
            feedback_id=f"FB-{now.strftime('%Y%m%d')}-{digest}",
            room_id=message.room_id,
            account_id=message.account_id,
            submitter=message.sender_name,
            feedback_type=str(extracted.get("feedback_type", "需求/反馈")),
            title=title or "未填写具体内容",
            description=description,
            priority=str(extracted.get("priority", "P2")),
            status="待确认",
            source_message_ids=(message.message_id,),
            confidence=float(extracted.get("confidence", 0.55)),
            need_more_info=bool(extracted.get("need_more_info", len(description) < 8)),
            created_at=now,
            updated_at=now,
        )
        self.database.save_feedback(item)
        return item
