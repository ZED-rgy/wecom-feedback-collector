from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse

from .adapters.bot import DryRunBot, build_bot
from .adapters.sender import DryRunSender, build_sender
from .config import ConfigValidationError, Settings, save_env
from .db import Database
from .models import RawMessage, SendJob
from .services.workflow import WorkflowService
from .services.schedule import next_schedule_at


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger("wecom_feedback.webapp")
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class RequestValidationError(ValueError):
    pass


def _next_summary_at(settings: Settings) -> str:
    value = next_schedule_at(settings)
    return value.isoformat() if value else ""


def _activity(database: Database, room_key: str, limit: int = 40) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for message in database.list_messages(room_key, limit=limit):
        events.append(
            {
                "id": message["message_id"],
                "kind": "message",
                "title": f"收到 {message['sender_name']} 的群消息",
                "detail": message["content"],
                "status": "success",
                "created_at": message["created_at"],
            }
        )
    for job in database.list_jobs(limit=limit):
        status = str(job["status"])
        activity_status = (
            "error" if status in {"failed", "unconfirmed"}
            else ("success" if status == "sent" else "pending")
        )
        events.append(
            {
                "id": job["job_id"],
                "kind": "send",
                "title": {"sent": "摘要已发送", "pending": "摘要等待发送", "cancelled": "摘要任务已取消", "failed": "摘要发送失败", "unconfirmed": "摘要发送待人工确认"}.get(
                    status, "摘要发送任务"
                ),
                "detail": job["last_error"] or str(job["content"]).splitlines()[0],
                "status": activity_status,
                "created_at": job["scheduled_at"],
            }
        )
    events.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return events[:limit]


def _dashboard_snapshot(settings: Settings, database: Database) -> dict[str, object]:
    room_key = settings.target_room_id or settings.target_group_name
    feedback = database.list_feedback(room_key)
    jobs = database.list_jobs()
    local = LOCAL_RECEIVER.status()
    runtime = SUMMARY_SCHEDULER.status()
    sync = database.smart_table_sync_counts(room_key)
    today = datetime.now().astimezone().date()
    today_feedback = sum(1 for item in feedback if item.created_at.astimezone().date() == today)
    status_counts = Counter(item.status for item in feedback)
    alerts: list[dict[str, str]] = []
    if local["last_error"]:
        alerts.append({"level": "error", "title": "群消息接收异常", "detail": str(local["last_error"])})
    if runtime["last_error"]:
        alerts.append({"level": "error", "title": "摘要调度异常", "detail": str(runtime["last_error"])})
    current_jobs = [
        job for job in jobs
        if job.get("room_id") == room_key
        and job.get("target_group_name") == settings.target_group_name
    ]
    failed_jobs = [
        job for job in current_jobs
        if job["last_error"] and job["status"] not in {"sent", "cancelled"}
    ]
    if failed_jobs:
        alerts.append({"level": "warning", "title": "存在发送失败任务", "detail": failed_jobs[0]["last_error"]})
    pipeline = [
        {"key": "receive", "name": "接收群消息", "status": "ok" if local["running"] and not local["last_error"] else "error", "detail": f"累计处理 {local['processed_total']} 条"},
        {"key": "organize", "name": "整理反馈", "status": "ok", "detail": f"已形成 {len(feedback)} 条记录"},
        {"key": "table", "name": "写入智能表格", "status": "ok" if settings.table_integration_enabled and sync["pending"] == 0 else "warning", "detail": f"已同步 {sync['synced']}，待同步 {sync['pending']}"},
        {"key": "summary", "name": "生成摘要", "status": "ok" if runtime["running"] else "error", "detail": f"下次 {_next_summary_at(settings) or '未设置'}"},
        {"key": "send", "name": "发送到群", "status": "ok" if settings.auto_send_enabled else "paused", "detail": "自动发送已开启" if settings.auto_send_enabled else "自动发送已暂停"},
    ]
    return {
        "group_name": settings.target_group_name,
        "account_name": "、".join(settings.target_account_names),
        "today_feedback": today_feedback,
        "total_feedback": len(feedback),
        "pending_review": status_counts.get("待确认", 0),
        "pending_sync": sync["pending"],
        "pending_jobs": sum(1 for job in current_jobs if job["status"] in {"pending", "claimed"}),
        "next_summary_at": _next_summary_at(settings),
        "auto_send_enabled": settings.auto_send_enabled,
        "local_reader": local,
        "scheduler": runtime,
        "sync": sync,
        "pipeline": pipeline,
        "alerts": alerts,
        "recent_activity": _activity(database, room_key, 8),
    }


def _feedback_payload(database: Database, item: object) -> dict[str, object]:
    values = dict(item.__dict__)
    feedback_id = str(values["feedback_id"])
    values["smart_table_synced"] = bool(database.get_state(f"smart_table_synced:{feedback_id}"))
    values["included_in_summary"] = values["status"] not in {"已忽略", "已完成"}
    return values


class LocalReceiverController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._desired = False
        self._last_error = ""
        self._last_poll_at = ""
        self._last_processed = 0
        self._processed_total = 0

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "last_error": self._last_error,
                "last_poll_at": self._last_poll_at,
                "last_processed": self._last_processed,
                "processed_total": self._processed_total,
            }

    def start(self) -> dict[str, object]:
        with self._lock:
            self._desired = True
            if self._thread and self._thread.is_alive():
                return self.status_unlocked()
            self._stop.clear()
            self._last_error = ""
            self._thread = threading.Thread(target=self._run, name="wecom-local-reader", daemon=True)
            self._thread.start()
            return self.status_unlocked()

    def status_unlocked(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_error": self._last_error,
            "last_poll_at": self._last_poll_at,
            "last_processed": self._last_processed,
            "processed_total": self._processed_total,
        }

    def stop(self) -> dict[str, object]:
        self._stop.set()
        with self._lock:
            self._desired = False
            return self.status_unlocked()

    def desired(self) -> bool:
        with self._lock:
            return self._desired

    def restart(self) -> dict[str, object]:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=15)
        return self.start()

    def _run(self) -> None:
        from .adapters.windows_local_db import WindowsWeComLocalDbReceiver

        settings = Settings.from_env()
        database = Database(settings.database_path)
        database.init_schema()
        workflow = WorkflowService(settings, database, build_bot(settings))
        receiver = WindowsWeComLocalDbReceiver(settings, workflow.process_message)
        while not self._stop.is_set():
            try:
                processed = receiver.poll_once()
                error = ""
            except Exception as exc:
                processed = 0
                error = str(exc)
            with self._lock:
                self._last_processed = processed
                self._processed_total += processed
                self._last_error = error
                self._last_poll_at = datetime.now(timezone.utc).isoformat()
            self._stop.wait(max(2, settings.poll_interval_seconds))


LOCAL_RECEIVER = LocalReceiverController()


class SummarySchedulerController:
    """Schedule summaries and dispatch pending jobs in the dashboard process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._desired = False
        self._last_error = ""
        self._last_cycle_at = ""
        self._last_result: dict[str, object] = {}

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "last_error": self._last_error,
                "last_cycle_at": self._last_cycle_at,
                "last_result": self._last_result,
            }

    def start(self) -> dict[str, object]:
        with self._lock:
            self._desired = True
            if self._thread and self._thread.is_alive():
                return self._status_unlocked()
            self._stop.clear()
            self._last_error = ""
            self._thread = threading.Thread(target=self._run, name="wecom-summary-scheduler", daemon=True)
            self._thread.start()
            return self._status_unlocked()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        with self._lock:
            self._desired = False
            return self._status_unlocked()

    def desired(self) -> bool:
        with self._lock:
            return self._desired

    def _status_unlocked(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_error": self._last_error,
            "last_cycle_at": self._last_cycle_at,
            "last_result": self._last_result,
        }

    def _run(self) -> None:
        from .adapters.archive import NotConfiguredArchive
        from .runtime import CollectorRuntime

        while not self._stop.is_set():
            settings = Settings.from_env()
            try:
                database = Database(settings.database_path)
                database.init_schema()
                runtime = CollectorRuntime(
                    settings,
                    database,
                    NotConfiguredArchive(),
                    build_bot(settings),
                    build_sender(settings),
                )
                result = runtime.run_once()
                error = ""
            except Exception as exc:
                result = {}
                error = str(exc)
                logger.exception("summary scheduler cycle failed")
            with self._lock:
                self._last_result = result
                self._last_error = error
                self._last_cycle_at = datetime.now(timezone.utc).isoformat()
            self._stop.wait(max(2, settings.poll_interval_seconds))


SUMMARY_SCHEDULER = SummarySchedulerController()


class BackgroundWatchdog:
    """Restart a controller thread if an unexpected exception kills it."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wecom-background-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(10):
            try:
                if LOCAL_RECEIVER.desired() and not LOCAL_RECEIVER.status()["running"]:
                    LOCAL_RECEIVER.start()
                if SUMMARY_SCHEDULER.desired() and not SUMMARY_SCHEDULER.status()["running"]:
                    SUMMARY_SCHEDULER.start()
            except Exception:
                logger.exception("background controller watchdog failed")


BACKGROUND_WATCHDOG = BackgroundWatchdog()


class ManualSendController:
    """Run operator-confirmed sends after the WebView click has fully settled."""

    def __init__(self, settle_seconds: float = 2.0) -> None:
        self.settle_seconds = settle_seconds
        self._queue: Queue[tuple[Settings, SendJob]] = Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_job_id = ""
        self._last_job_id = ""
        self._last_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wecom-manual-send", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, settings: Settings, job: SendJob) -> None:
        self.start()
        self._queue.put((settings, job))

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "active_job_id": self._active_job_id,
                "last_job_id": self._last_job_id,
                "last_error": self._last_error,
                "queued": self._queue.qsize(),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                settings, job = self._queue.get(timeout=0.5)
            except Empty:
                continue
            with self._lock:
                self._active_job_id = job.job_id
                self._last_error = ""
            try:
                # The response that closes the confirmation modal must finish
                # before Win32 focus changes begin. Otherwise the WebView's
                # click/focus lifecycle can immediately steal focus back.
                if self._stop.wait(self.settle_seconds):
                    break
                database = Database(settings.database_path)
                database.init_schema()
                workflow = WorkflowService(settings, database, build_bot(settings))
                from .adapters.sender import build_manual_sender

                workflow.dispatch_claimed_job(job, build_manual_sender(settings))
            except Exception as exc:
                logger.exception("manual send worker failed for %s", job.job_id)
                with self._lock:
                    self._last_error = str(exc)
            finally:
                with self._lock:
                    self._last_job_id = job.job_id
                    self._active_job_id = ""
                self._queue.task_done()


MANUAL_SEND_QUEUE = ManualSendController()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WeComFeedbackDashboard/0.1"

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestValidationError("请求长度无效") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestValidationError("请求内容过大")
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestValidationError("请求 JSON 格式无效") from exc
        if not isinstance(payload, dict):
            raise RequestValidationError("请求主体必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        settings = Settings.from_env()
        database = Database(settings.database_path)
        database.init_schema()
        if path == "/api/settings":
            from .startup import is_startup_enabled

            public_settings = settings.public_dict()
            public_settings["start_with_windows"] = is_startup_enabled()
            return self._json(public_settings)
        if path == "/api/health":
            from .health import check_health

            return self._json(check_health(settings, database).as_dict())
        if path == "/api/dashboard":
            return self._json(_dashboard_snapshot(settings, database))
        if path == "/api/feedback":
            room_key = settings.target_room_id or settings.target_group_name
            return self._json([_feedback_payload(database, item) for item in database.list_feedback(room_key)])
        if path == "/api/messages":
            room_key = settings.target_room_id or settings.target_group_name
            return self._json(database.list_messages(room_key))
        if path == "/api/jobs":
            return self._json(database.list_jobs())
        if path == "/api/activity":
            room_key = settings.target_room_id or settings.target_group_name
            return self._json(_activity(database, room_key))
        if path == "/api/summary/preview":
            from .services.reporting import build_report
            from .services.summary import select_summary_items

            room_key = settings.target_room_id or settings.target_group_name
            candidates = [
                item for item in database.list_feedback(room_key)
                if item.status not in {"已忽略", "已完成"}
            ]
            selected = select_summary_items(candidates)
            report = build_report(settings, database, build_bot(settings))
            return self._json(
                {
                    "content": report["content"],
                    "items": [_feedback_payload(database, item) for item in selected],
                    "next_summary_at": _next_summary_at(settings),
                    "target_group": settings.target_group_name,
                    "auto_send_enabled": settings.auto_send_enabled,
                    "stats": {
                        key: report[key]
                        for key in ("today_new", "pending_confirmation", "in_progress", "completed_today", "total")
                    },
                    "source": report["source"],
                    "warning": report["warning"],
                }
            )
        if path == "/api/local-reader/status":
            return self._json(LOCAL_RECEIVER.status())
        if path == "/api/runtime/status":
            return self._json(SUMMARY_SCHEDULER.status())
        if path == "/api/manual-send/status":
            return self._json(MANUAL_SEND_QUEUE.status())
        if path in {"/", "/index.html"}:
            body = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/settings":
                try:
                    save_env(payload)
                except ConfigValidationError as exc:
                    return self._json({"error": str(exc)}, 400)
                if "start_with_windows" in payload:
                    from .startup import set_startup_enabled

                    set_startup_enabled(bool(payload["start_with_windows"]))
                if Settings.from_env().local_db_enabled:
                    LOCAL_RECEIVER.restart()
                else:
                    LOCAL_RECEIVER.stop()
                return self._json(Settings.from_env().public_dict())
            settings = Settings.from_env()
            database = Database(settings.database_path)
            database.init_schema()
            workflow = WorkflowService(settings, database, DryRunBot())
            if path in {"/api/group/validate", "/api/group/switch"}:
                from .adapters.windows_local_db import WindowsWeComLocalDbReceiver

                group_name = str(payload.get("group_name", "")).strip()
                group_remark = str(payload.get("group_remark", "")).strip()
                if not group_name:
                    return self._json({"error": "请输入群聊名称"}, 400)
                candidate = replace(
                    settings,
                    target_room_id="",
                    target_group_name=group_name,
                    target_group_remark=group_remark,
                )
                diagnostic = WindowsWeComLocalDbReceiver(candidate, lambda _message: False).diagnose().as_dict()
                if path.endswith("/validate"):
                    return self._json(diagnostic)
                if not diagnostic.get("ready"):
                    return self._json({"error": diagnostic.get("error") or "未在本机企微中找到该群", "diagnostic": diagnostic}, 400)
                old_group = settings.target_group_name
                old_room = settings.target_room_id or old_group
                cancelled_jobs = database.cancel_jobs_for_target(
                    old_room,
                    old_group,
                    "监听群已切换，旧群发送任务已取消",
                )
                values = settings.public_dict()
                values.update(
                    {
                        "target_room_id": "",
                        "target_group_name": group_name,
                        "target_group_remark": group_remark,
                        "archive_secret": "",
                        "table_bot_secret": "",
                    }
                )
                save_env(values)
                if candidate.local_db_enabled:
                    LOCAL_RECEIVER.restart()
                return self._json(
                    {
                        "switched": True,
                        "old_group": old_group,
                        "new_group": group_name,
                        "cancelled_jobs": cancelled_jobs,
                        "diagnostic": diagnostic,
                    }
                )
            if path == "/api/local-reader/diagnose":
                from .adapters.windows_local_db import WindowsWeComLocalDbReceiver

                receiver = WindowsWeComLocalDbReceiver(settings, lambda _message: False)
                return self._json(receiver.diagnose().as_dict())
            if path == "/api/local-reader/poll":
                from .adapters.windows_local_db import WindowsWeComLocalDbReceiver

                real_workflow = WorkflowService(settings, database, build_bot(settings))
                receiver = WindowsWeComLocalDbReceiver(settings, real_workflow.process_message)
                return self._json({"processed": receiver.poll_once()})
            if path == "/api/local-reader/start":
                return self._json(LOCAL_RECEIVER.start())
            if path == "/api/local-reader/stop":
                return self._json(LOCAL_RECEIVER.stop())
            if path in {"/api/feedback/update", "/api/feedback/ignore"}:
                feedback_id = str(payload.get("feedback_id", "")).strip()
                if not feedback_id:
                    return self._json({"error": "feedback_id is required"}, 400)
                values = dict(payload.get("values", {})) if isinstance(payload.get("values"), dict) else {}
                if path.endswith("/ignore"):
                    values = {"status": "已忽略"}
                item = database.update_feedback(feedback_id, values)
                if item is None:
                    return self._json({"error": "feedback not found"}, 404)
                sync_error = ""
                if settings.table_integration_enabled and not settings.dry_run:
                    try:
                        build_bot(settings).upsert_feedback(item)
                        database.set_state(f"smart_table_synced:{feedback_id}", "1")
                    except Exception as exc:
                        database.delete_state(f"smart_table_synced:{feedback_id}")
                        sync_error = str(exc)
                return self._json({"item": _feedback_payload(database, item), "sync_error": sync_error})
            if path == "/api/feedback/resync":
                feedback_id = str(payload.get("feedback_id", "")).strip()
                item = database.get_feedback(feedback_id)
                if item is None:
                    return self._json({"error": "feedback not found"}, 404)
                if not settings.table_integration_enabled:
                    return self._json({"error": "智能表格写入尚未启用"}, 400)
                if settings.dry_run:
                    return self._json({"error": "dry-run 模式不会写入智能表格"}, 400)
                database.delete_state(f"smart_table_synced:{feedback_id}")
                build_bot(settings).upsert_feedback(item)
                database.set_state(f"smart_table_synced:{feedback_id}", "1")
                return self._json({"synced": True})
            if path in {"/api/summary/create", "/api/summary/send-now"}:
                feedback_ids = payload.get("feedback_ids")
                selected_ids = [str(value) for value in feedback_ids] if isinstance(feedback_ids, list) else None
                content = str(payload.get("content", "")).strip() or None
                real_workflow = WorkflowService(settings, database, build_bot(settings))
                job = real_workflow.schedule_summary(
                    scheduled_at=datetime.now(timezone.utc), feedback_ids=selected_ids, content=content
                )
                sent = False
                if path.endswith("/send-now"):
                    claimed = database.claim_job(job.job_id)
                    if claimed is None:
                        return self._json({"error": "发送任务未能锁定，请刷新后重试"}, 409)
                    MANUAL_SEND_QUEUE.enqueue(settings, claimed)
                    return self._json(
                        {"job_id": job.job_id, "sent": False, "queued": True, "status": "claimed"},
                        202,
                    )
                return self._json({"job_id": job.job_id, "sent": sent, "status": database.get_job(job.job_id).status})
            if path == "/api/jobs/cancel":
                return self._json({"cancelled": database.cancel_job(str(payload.get("job_id", "")))})
            if path == "/api/jobs/retry":
                job_id = str(payload.get("job_id", ""))
                job = database.get_job(job_id)
                expected_room = settings.target_room_id or settings.target_group_name
                if job and (job.room_id != expected_room or job.target_group_name != settings.target_group_name):
                    return self._json({"error": "任务所属群已变化，请重新生成当前群摘要"}, 409)
                return self._json({"retried": database.retry_job(job_id)})
            if path == "/api/demo-ingest":
                content = str(payload.get("content", "")).strip()
                if not content:
                    return self._json({"error": "content is required"}, 400)
                message = RawMessage(
                    message_id=f"web-demo-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
                    seq=0,
                    account_id="web-demo-customer",
                    room_id=settings.target_room_id or settings.target_group_name or "demo-room",
                    group_name=settings.target_group_name or "演示客户群",
                    group_remark=settings.target_group_remark,
                    sender_id="web-demo-customer",
                    sender_name="网页演示",
                    message_type="text",
                    raw_content=content,
                    content=content,
                    mentioned_account=True,
                )
                return self._json({"accepted": workflow.process_message(message)})
            if path == "/api/demo-summary":
                job = workflow.schedule_summary()
                sent = workflow.dispatch_due_jobs(DryRunSender())
                return self._json({"job_id": job.job_id, "sent": sent})
            self._json({"error": "not found"}, 404)
        except (RequestValidationError, ConfigValidationError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def log_message(self, format: str, *args: object) -> None:
        return None


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_dashboard_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return DashboardServer((host, port), DashboardHandler)


def start_background_controllers() -> None:
    if Settings.from_env().local_db_enabled:
        LOCAL_RECEIVER.start()
    SUMMARY_SCHEDULER.start()
    MANUAL_SEND_QUEUE.start()
    BACKGROUND_WATCHDOG.start()


def stop_background_controllers() -> None:
    BACKGROUND_WATCHDOG.stop()
    LOCAL_RECEIVER.stop()
    SUMMARY_SCHEDULER.stop()
    MANUAL_SEND_QUEUE.stop()


def run_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_dashboard_server(host, port)
    print(f"dashboard running at http://{host}:{port}")
    start_background_controllers()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_background_controllers()
        server.server_close()
