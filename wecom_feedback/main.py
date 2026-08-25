from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .adapters.bot import DryRunBot
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
            room_id=settings.target_room_id or "demo-room",
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


if __name__ == "__main__":
    main()
