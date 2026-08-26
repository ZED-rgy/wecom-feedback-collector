import unittest
from datetime import datetime, timezone

from wecom_feedback.models import FeedbackItem
from wecom_feedback.services.summary import render_feedback_summary


def item(feedback_id: str, title: str, source: str) -> FeedbackItem:
    now = datetime.now(timezone.utc)
    return FeedbackItem(
        feedback_id=feedback_id,
        room_id="测试群",
        account_id="sender",
        submitter="客户",
        feedback_type="使用问题",
        title=title,
        description=title,
        priority="P2",
        status="待确认",
        source_message_ids=(source,),
        confidence=0.8,
        need_more_info=False,
        created_at=now,
        updated_at=now,
    )


class SummaryTests(unittest.TestCase):
    def test_prefers_local_source_and_merges_duplicates(self):
        summary = render_feedback_summary(
            [
                item("ui", "OCR 噪声", "ui-1"),
                item("local-1", "登录失败", "local-1"),
                item("local-2", "登录失败", "local-2"),
            ]
        )
        self.assertNotIn("OCR 噪声", summary)
        self.assertIn("合并为 1 项", summary)
        self.assertIn("出现2次", summary)


if __name__ == "__main__":
    unittest.main()
