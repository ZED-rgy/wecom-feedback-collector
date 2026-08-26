from __future__ import annotations

"""Smart-table adapter backed by the official WeCom CLI.

The Windows account remains the receive/send channel for the customer group.
This adapter is only used for the separately authorized bot that owns the
destination smart table.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Sequence

from ..config import Settings
from ..models import FeedbackItem
from ..services.summary import render_feedback_summary


class SmartTableError(RuntimeError):
    """Raised when the local WeCom CLI cannot update the smart table."""


def _decode_cli_output(value: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _text_value(value: str) -> str:
    return json.dumps([{"text": value, "type": "text"}], ensure_ascii=False, separators=(",", ":"))


def _date_value(value: datetime) -> str:
    local = value.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S")


class CliSmartTableBot:
    """Write feedback rows and render summaries through ``wecom-cli``."""

    def __init__(self, settings: Settings, executable: str | None = None, timeout_seconds: int = 45):
        self.settings = settings
        self.executable = executable or shutil.which("wecom-cli") or "wecom-cli"
        self.timeout_seconds = timeout_seconds
        self._reporting_fields_ready = False

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
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except FileNotFoundError as exc:
            raise SmartTableError("未找到 wecom-cli，请先安装 @wecom/cli") from exc
        except subprocess.TimeoutExpired as exc:
            raise SmartTableError("企微智能表格操作超时") from exc
        stdout = _decode_cli_output(result.stdout)
        stderr = _decode_cli_output(result.stderr)
        if result.returncode != 0:
            output = (stderr or stdout).strip()
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
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SmartTableError("企微 CLI 返回了无法解析的结果") from exc
        if isinstance(response, dict) and response.get("errcode", 0) not in (0, None):
            raise SmartTableError(str(response.get("errmsg") or response.get("errcode")))
        if isinstance(response, dict):
            response.pop("extra_identity_context", None)
        return response

    @staticmethod
    def _plain_value(value: object) -> str:
        if isinstance(value, list):
            return "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in value
            ).strip()
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _parse_datetime(value: object, fallback: object = "") -> datetime:
        text = str(value or fallback or "").strip()
        for candidate in (text, text.replace("Z", "+00:00")):
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed if parsed.tzinfo else parsed.astimezone()
            except ValueError:
                continue
        return datetime.now(timezone.utc).astimezone()

    def list_fields(self) -> list[dict[str, Any]]:
        response = self._run(
            ("fields", "list"),
            {"sheet_title": "反馈记录", "type": "fields"},
        )
        fields = response.get("fields", [])
        return [field for field in fields if isinstance(field, dict)]

    def list_records(self) -> list[dict[str, Any]]:
        response = self._run(
            ("records", "list"),
            {"sheet_title": "反馈记录", "type": "records", "key_type": "field_title", "limit": 1000},
        )
        records = response.get("records", [])
        return [record for record in records if isinstance(record, dict)]

    def ensure_reporting_fields(self) -> list[str]:
        """Add the small set of text fields needed for stable reporting."""
        if self._reporting_fields_ready:
            return []
        existing = {str(field.get("field_title", "")) for field in self.list_fields()}
        missing = [name for name in ("任务编号", "状态", "优先级") if name not in existing]
        if missing:
            self._run(
                ("fields", "add"),
                {
                    "sheet_title": "反馈记录",
                    "type": "add",
                    "fields": [{"field_title": name, "field_type": "text"} for name in missing],
                },
            )
        self._reporting_fields_ready = True
        return missing

    def _feedback_values(self, item: FeedbackItem) -> dict[str, object]:
        return {
            "提出日期": _date_value(item.created_at),
            "问题描述/补充": _text_value(item.description),
            "反馈人": _text_value(item.submitter),
            "问题类型": _text_value(item.feedback_type),
            "是否已解决": item.status == "已完成",
            "备注": _text_value(f"优先级：{item.priority}；状态：{item.status}"),
            "来源群": _text_value(self.settings.target_group_name or item.room_id),
            "来源消息ID": _text_value(",".join(item.source_message_ids)),
            "任务编号": _text_value(item.feedback_id),
            "状态": _text_value(item.status),
            "优先级": _text_value(item.priority),
        }

    def upsert_feedback(self, item: FeedbackItem) -> None:
        if not self.settings.table_integration_enabled or self.settings.dry_run:
            return
        self.ensure_reporting_fields()
        values = self._feedback_values(item)
        source_ids = set(item.source_message_ids)
        existing_id = ""
        for record in self.list_records():
            record_values = record.get("values", {})
            if not isinstance(record_values, dict):
                continue
            record_sources = {
                part.strip()
                for part in self._plain_value(record_values.get("来源消息ID")).split(",")
                if part.strip()
            }
            task_id = self._plain_value(record_values.get("任务编号"))
            if task_id == item.feedback_id or source_ids.intersection(record_sources):
                existing_id = str(record.get("record_id", ""))
                break
        command = ("records", "update") if existing_id else ("records", "add")
        record: dict[str, object] = {"values": values}
        if existing_id:
            record["record_id"] = existing_id
        self._run(
            command,
            {
                "sheet_title": "反馈记录",
                "key_type": "field_title",
                "type": "update" if existing_id else "add",
                "records": [record],
            },
        )

    def reporting_snapshot(self, since: datetime | None = None, limit: int = 5) -> dict[str, object]:
        """Return group-scoped metrics and details with the table as source of truth."""
        self.ensure_reporting_fields()
        now = datetime.now().astimezone()
        today = now.date()
        group_name = self.settings.target_group_name
        tasks: list[dict[str, object]] = []
        for record in self.list_records():
            values = record.get("values", {})
            if not isinstance(values, dict):
                continue
            source_group = self._plain_value(values.get("来源群"))
            if group_name and source_group != group_name:
                continue
            notes = self._plain_value(values.get("备注"))
            status_match = re.search(r"状态[：:]\s*([^；;，,\s]+)", notes)
            priority_match = re.search(r"优先级[：:]\s*([^；;，,\s]+)", notes)
            status = self._plain_value(values.get("状态")) or (status_match.group(1) if status_match else "待确认")
            if values.get("是否已解决") is True:
                status = "已完成"
            priority = self._plain_value(values.get("优先级")) or (
                priority_match.group(1) if priority_match else "P2"
            )
            created_at = self._parse_datetime(values.get("提出日期"), record.get("create_time"))
            updated_at = self._parse_datetime(record.get("update_time"), created_at.isoformat())
            tasks.append(
                {
                    "task_id": self._plain_value(values.get("任务编号")) or f"TABLE-{record.get('record_id', '')}",
                    "record_id": str(record.get("record_id", "")),
                    "title": self._plain_value(values.get("问题描述/补充")) or "未命名任务",
                    "submitter": self._plain_value(values.get("反馈人")),
                    "status": status,
                    "priority": priority,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        active = [task for task in tasks if task["status"] != "已忽略"]
        recent = [task for task in active if since is None or task["created_at"] >= since.astimezone()]
        recent.sort(key=lambda task: task["created_at"], reverse=True)
        focus = [
            task for task in active
            if task["status"] not in {"已完成", "已忽略"} and task["priority"] in {"P0", "P1"}
        ]
        focus.sort(key=lambda task: (task["priority"], task["created_at"]))
        return {
            "source": "smart_table",
            "total": len(active),
            "today_new": sum(1 for task in active if task["created_at"].date() == today),
            "pending_confirmation": sum(1 for task in active if task["status"] == "待确认"),
            "in_progress": sum(1 for task in active if task["status"] in {"处理中", "进行中"}),
            "completed_today": sum(
                1 for task in active if task["status"] == "已完成" and task["updated_at"].date() == today
            ),
            "recent": recent[:limit],
            "focus": focus[:limit],
            "tasks": active,
        }

    def render_summary(self, items: Sequence[FeedbackItem]) -> str:
        return render_feedback_summary(items)
