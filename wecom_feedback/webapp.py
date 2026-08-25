from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters.bot import DryRunBot
from .adapters.sender import DryRunSender
from .config import Settings, save_env
from .db import Database
from .models import RawMessage
from .services.workflow import WorkflowService


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


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
                return self._json(Settings.from_env().public_dict())
            settings = Settings.from_env()
            database = Database(settings.database_path)
            database.init_schema()
            workflow = WorkflowService(settings, database, DryRunBot())
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
