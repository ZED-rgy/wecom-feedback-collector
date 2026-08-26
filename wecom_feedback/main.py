from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .adapters.bot import DryRunBot, build_bot
from .adapters.sender import DryRunSender
from .config import Settings
from .db import Database
from .health import check_health
from .models import RawMessage
from .services.feedback import FeedbackService
from .services.ingestion import IngestionService
from .services.workflow import WorkflowService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WeCom group feedback collector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="create the SQLite schema")
    subparsers.add_parser("health", help="print local configuration and database health")
    demo = subparsers.add_parser("demo-ingest", help="ingest one local demo message")
    demo.add_argument("--content", required=True)
    demo.add_argument("--sender", default="演示客户")
    subparsers.add_parser("demo-summary", help="create and dispatch one dry-run summary")
    web = subparsers.add_parser("web", help="start the local configuration dashboard")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    desktop = subparsers.add_parser("desktop", help="run as a Windows tray application")
    desktop.add_argument("--host", default="127.0.0.1")
    desktop.add_argument("--port", type=int, default=8765)
    desktop.add_argument("--no-browser", action="store_true", help="do not open the dashboard at startup")
    run = subparsers.add_parser("run", help="run the long-lived collector loop")
    run.add_argument("--once", action="store_true", help="execute one cycle and exit")
    run.add_argument("--poll-interval", type=int, default=None)
    ui = subparsers.add_parser("run-ui", help="read @mentions from the currently open WeCom group window")
    ui.add_argument("--poll-interval", type=float, default=2.0)
    ui.add_argument("--once", action="store_true", help="poll once and exit")
    local = subparsers.add_parser(
        "run-local", help="read @mentions from the signed-in Windows WeCom local database"
    )
    local.add_argument("--poll-interval", type=float, default=None)
    local.add_argument("--once", action="store_true", help="poll once and exit")
    subparsers.add_parser("diagnose-local", help="diagnose Windows local database access without exporting messages")
    return parser


def main(argv: list[str] | None = None) -> None:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.init_schema()
    args = build_parser().parse_args(argv)

    if args.command == "init-db":
        print(f"database initialized: {settings.database_path}")
        return
    if args.command == "health":
        print(json.dumps(check_health(settings, database).as_dict(), ensure_ascii=False, indent=2))
        return
    if args.command == "demo-ingest":
        message = RawMessage(
            message_id=f"demo-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            seq=0,
            account_id="demo-customer",
            room_id=settings.target_room_id or settings.target_group_name or "demo-room",
            group_name=settings.target_group_name or "演示客户群",
            group_remark=settings.target_group_remark,
            sender_id="demo-customer",
            sender_name=args.sender,
            message_type="text",
            raw_content=args.content,
            content=args.content,
            mentioned_account=True,
        )
        ingestion = IngestionService(settings, database)
        if not ingestion.ingest(message):
            print("message was ignored; configure target room or mention target account")
            return
        item = FeedbackService(settings, database).create_from_message(message)
        print(json.dumps(item.__dict__ if item else {"duplicate": True}, ensure_ascii=False, default=str, indent=2))
        return
    if args.command == "demo-summary":
        workflow = WorkflowService(settings, database, DryRunBot())
        job = workflow.schedule_summary()
        sent = workflow.dispatch_due_jobs(DryRunSender())
        print(json.dumps({"job_id": job.job_id, "sent": sent}, ensure_ascii=False, indent=2))
        return
    if args.command == "web":
        from .webapp import run_dashboard

        run_dashboard(args.host, args.port)
        return
    if args.command == "desktop":
        from .desktop import run_desktop

        run_desktop(args.host, args.port, open_browser=not args.no_browser)
        return
    if args.command == "run":
        from .adapters.archive import NotConfiguredArchive
        from .runtime import CollectorRuntime

        runtime = CollectorRuntime(settings, database, NotConfiguredArchive(), build_bot(settings), DryRunSender())
        if args.once:
            print(json.dumps(runtime.run_once(), ensure_ascii=False, indent=2))
        else:
            runtime.run_forever(args.poll_interval)
    if args.command == "run-ui":
        from .adapters.windows_ui import WindowsUiError
        from .adapters.windows_ui_receiver import WindowsUiReceiverConfig, WindowsWeComUiReceiver

        workflow = WorkflowService(settings, database, build_bot(settings))
        receiver = WindowsWeComUiReceiver(
            settings,
            workflow.process_message,
            WindowsUiReceiverConfig(poll_seconds=args.poll_interval),
        )
        if args.once:
            try:
                accepted = receiver.poll_once()
            except WindowsUiError as exc:
                print(json.dumps({"accepted": 0, "error": str(exc)}, ensure_ascii=False))
            else:
                print(json.dumps({"accepted": accepted}, ensure_ascii=False))
        else:
            receiver.run_forever()
    if args.command in {"run-local", "diagnose-local"}:
        from .adapters.windows_local_db import WindowsWeComLocalDbReceiver

        workflow = WorkflowService(settings, database, build_bot(settings))
        receiver = WindowsWeComLocalDbReceiver(settings, workflow.process_message)
        if args.command == "diagnose-local":
            print(json.dumps(receiver.diagnose().as_dict(), ensure_ascii=False, indent=2))
        elif args.once:
            print(json.dumps({"processed": receiver.poll_once()}, ensure_ascii=False, indent=2))
        else:
            receiver.run_forever(args.poll_interval)


if __name__ == "__main__":
    main()
