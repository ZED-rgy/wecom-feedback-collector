from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..config import Settings
from ..db import Database
from ..models import FeedbackItem, RawMessage
from .ingestion import strip_mention


class FeedbackService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def create_from_message(self, message: RawMessage) -> FeedbackItem | None:
        if self.database.feedback_exists_for_message(message.message_id):
            return None
        description = strip_mention(message.content, self.settings.target_account_names).strip()
        description = description or message.content.strip()
        feedback_type = self._type_for(description)
        priority = "P1" if any(word in description for word in ("崩溃", "无法登录", "全部不可用")) else "P2"
        title = description[:48] + ("…" if len(description) > 48 else "")
        digest = hashlib.sha1(message.message_id.encode("utf-8")).hexdigest()[:10]
        now = datetime.now(timezone.utc)
        item = FeedbackItem(
            feedback_id=f"FB-{now.strftime('%Y%m%d')}-{digest}",
            room_id=message.room_id,
            account_id=message.account_id,
            submitter=message.sender_name,
            feedback_type=feedback_type,
            title=title or "未填写具体内容",
            description=description,
            priority=priority,
            status="待确认",
            source_message_ids=(message.message_id,),
            confidence=0.55,
            need_more_info=len(description) < 8,
            created_at=now,
            updated_at=now,
        )
        self.database.save_feedback(item)
        return item

    @staticmethod
    def _type_for(content: str) -> str:
        if any(word in content for word in ("建议", "希望", "能不能", "最好")):
            return "功能建议"
        if any(word in content for word in ("报错", "失败", "异常", "无法", "不显示")):
            return "使用问题"
        return "需求/反馈"
