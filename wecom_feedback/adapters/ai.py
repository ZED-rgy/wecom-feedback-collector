from __future__ import annotations

from typing import Protocol


class FeedbackExtractor(Protocol):
    def extract(self, content: str) -> dict[str, object]:
        """Return normalized feedback fields for one message."""


class RuleBasedFeedbackExtractor:
    """Safe local fallback; replace with an LLM-backed extractor later."""

    def extract(self, content: str) -> dict[str, object]:
        if any(word in content for word in ("建议", "希望", "能不能", "最好")):
            feedback_type = "功能建议"
        elif any(word in content for word in ("报错", "失败", "异常", "无法", "不显示")):
            feedback_type = "使用问题"
        else:
            feedback_type = "需求/反馈"
        priority = "P1" if any(word in content for word in ("崩溃", "无法登录", "全部不可用")) else "P2"
        return {
            "feedback_type": feedback_type,
            "priority": priority,
            "confidence": 0.55,
            "need_more_info": len(content) < 8,
        }
