from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters.bot import DryRunBot, build_bot
from .adapters.sender import DryRunSender
from .config import Settings, save_env
from .db import Database
from .models import RawMessage
from .services.workflow import WorkflowService


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class LocalReceiverController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
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
            return self.status_unlocked()

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
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        settings = Settings.from_env()
        database = Database(settings.database_path)
        database.init_schema()
        if path == "/api/settings":
            return self._json(settings.public_dict())
        if path == "/api/health":
            from .health import check_health

            return self._json(check_health(settings, database).as_dict())
        if path == "/api/feedback":
            room_key = settings.target_room_id or settings.target_group_name
            return self._json([item.__dict__ for item in database.list_feedback(room_key)])
        if path == "/api/jobs":
            return self._json(database.list_jobs())
        if path == "/api/local-reader/status":
            return self._json(LOCAL_RECEIVER.status())
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
                save_env(payload)
                if Settings.from_env().local_db_enabled:
                    LOCAL_RECEIVER.restart()
                else:
                    LOCAL_RECEIVER.stop()
                return self._json(Settings.from_env().public_dict())
            settings = Settings.from_env()
            database = Database(settings.database_path)
            database.init_schema()
            workflow = WorkflowService(settings, database, DryRunBot())
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
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def log_message(self, format: str, *args: object) -> None:
        return None


def run_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"dashboard running at http://{host}:{port}")
    if Settings.from_env().local_db_enabled:
        LOCAL_RECEIVER.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LOCAL_RECEIVER.stop()
        server.server_close()
