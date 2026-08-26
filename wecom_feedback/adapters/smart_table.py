from __future__ import annotations

"""Smart-table adapter backed by the official WeCom CLI.

The Windows account remains the receive/send channel for the customer group.
This adapter is only used for the separately authorized bot that owns the
destination smart table.
"""

import json
import shutil
import subprocess
from datetime import datetime
from typing import Any, Sequence

from ..config import Settings
from ..models import FeedbackItem


class SmartTableError(RuntimeError):
    """Raised when the local WeCom CLI cannot update the smart table."""


def _text_value(value: str) -> str:
    return json.dumps([{"text": value, "type": "text"}], ensure_ascii=False, separators=(",", ":"))


def _date_value(value: datetime) -> str:
    local = value.astimezone()
    return local.strftime("%Y-%m-%d 00:00:00")


class CliSmartTableBot:
    """Write feedback rows and render summaries through ``wecom-cli``."""

    def __init__(self, settings: Settings, executable: str | None = None, timeout_seconds: int = 45):
        self.settings = settings
        self.executable = executable or shutil.which("wecom-cli") or "wecom-cli"
        self.timeout_seconds = timeout_seconds

    def _run(self, command: Sequence[str], payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.smart_table_url:
            raise SmartTableError("WECOM_SMART_TABLE_URL 未配置")
        body = dict(payload)
        body["docid"] = self.settings.smart_table_url
        args = [self.executable, "smartsheet", *command, "--json", json.dumps(body, ensure_ascii=False)]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except FileNotFoundError as exc:
            raise SmartTableError("未找到 wecom-cli，请先安装 @wecom/cli") from exc
        except subprocess.TimeoutExpired as exc:
            raise SmartTableError("企微智能表格操作超时") from exc
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            try:
                error_payload = json.loads(output)
            except json.JSONDecodeError:
                error_payload = None
            if isinstance(error_payload, dict):
                error = error_payload.get("error")
                if isinstance(error, dict) and error.get("message"):
                    raise SmartTableError(str(error["message"]))
            detail = output.splitlines()
            raise SmartTableError(detail[-1] if detail else f"wecom-cli exit code {result.returncode}")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SmartTableError("企微 CLI 返回了无法解析的结果") from exc
        if isinstance(response, dict) and response.get("errcode", 0) not in (0, None):
            raise SmartTableError(str(response.get("errmsg") or response.get("errcode")))
        return response

    def upsert_feedback(self, item: FeedbackItem) -> None:
        if not self.settings.table_integration_enabled or self.settings.dry_run:
            return
        values = {
            "提出日期": _date_value(item.created_at),
            "问题描述/补充": _text_value(item.description),
            "反馈人": _text_value(item.submitter),
            "问题类型": _text_value(item.feedback_type),
            # CLI validates typed fields from the JSON value itself.  Do not
            # encode checkbox values as strings (text "false" is rejected).
            "是否已解决": False,
            "备注": _text_value(f"优先级：{item.priority}；状态：{item.status}"),
            "来源群": _text_value(self.settings.target_group_name or item.room_id),
            "来源消息ID": _text_value(",".join(item.source_message_ids)),
        }
        self._run(
            ("records", "add"),
            {"sheet_title": "反馈记录", "key_type": "field_title", "type": "add", "records": [{"values": values}]},
        )

    def render_summary(self, items: Sequence[FeedbackItem]) -> str:
        if not items:
            return "本时段暂无新的需求或问题。"
        lines = ["【客户群需求/问题摘要】"]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. [{item.priority}] {item.title}（{item.status}）")
        return "\n".join(lines)
