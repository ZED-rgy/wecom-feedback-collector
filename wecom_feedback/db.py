from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import FeedbackItem, RawMessage, SendJob


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_messages (
                    message_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    mentioned_account INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_raw_messages_room_time
                    ON raw_messages(room_id, created_at);
                CREATE TABLE IF NOT EXISTS feedback_items (
                    feedback_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_message_ids_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    need_more_info INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS send_jobs (
                    job_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_send_jobs_due
                    ON send_jobs(status, scheduled_at);
                """
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT state_value FROM sync_state WHERE state_key = ?", (key,)).fetchone()
            return row["state_value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_state(state_key, state_value) VALUES(?, ?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
                (key, value),
            )

    def insert_message(self, message: RawMessage) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO raw_messages
                (message_id, seq, account_id, room_id, sender_id, sender_name,
                 message_type, content, raw_content, mentioned_account, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.message_id,
                    message.seq,
                    message.account_id,
                    message.room_id,
                    message.sender_id,
                    message.sender_name,
                    message.message_type,
                    message.content,
                    message.raw_content,
                    int(message.mentioned_account),
                    message.created_at.isoformat(),
                    json.dumps(message.payload, ensure_ascii=False),
                ),
            )
            return cursor.rowcount == 1

    def feedback_exists_for_message(self, message_id: str) -> bool:
        needle = f'%"{message_id}"%'
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM feedback_items WHERE source_message_ids_json LIKE ? LIMIT 1", (needle,)
            ).fetchone()
            return row is not None

    def feedback_for_message(self, message_id: str) -> FeedbackItem | None:
        needle = f'%"{message_id}"%'
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_items WHERE source_message_ids_json LIKE ? LIMIT 1", (needle,)
            ).fetchone()
        if row is None:
            return None
        return FeedbackItem(
            feedback_id=row["feedback_id"],
            room_id=row["room_id"],
            account_id=row["account_id"],
            submitter=row["submitter"],
            feedback_type=row["feedback_type"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=row["status"],
            source_message_ids=tuple(json.loads(row["source_message_ids_json"])),
            confidence=float(row["confidence"]),
            need_more_info=bool(row["need_more_info"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_feedback(self, item: FeedbackItem) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO feedback_items
                (feedback_id, room_id, account_id, submitter, feedback_type, title, description,
                 priority, status, source_message_ids_json, confidence, need_more_info,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                  title=excluded.title, description=excluded.description,
                  priority=excluded.priority, status=excluded.status,
                  confidence=excluded.confidence, need_more_info=excluded.need_more_info,
                  updated_at=excluded.updated_at""",
                (
                    item.feedback_id,
                    item.room_id,
                    item.account_id,
                    item.submitter,
                    item.feedback_type,
                    item.title,
                    item.description,
                    item.priority,
                    item.status,
                    json.dumps(item.source_message_ids, ensure_ascii=False),
                    item.confidence,
                    int(item.need_more_info),
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )

    def list_feedback(self, room_id: str, limit: int = 100) -> list[FeedbackItem]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback_items WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
            return [
                FeedbackItem(
                    feedback_id=row["feedback_id"],
                    room_id=row["room_id"],
                    account_id=row["account_id"],
                    submitter=row["submitter"],
                    feedback_type=row["feedback_type"],
                    title=row["title"],
                    description=row["description"],
                    priority=row["priority"],
                    status=row["status"],
                    source_message_ids=tuple(json.loads(row["source_message_ids_json"])),
                    confidence=float(row["confidence"]),
                    need_more_info=bool(row["need_more_info"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in rows
            ]

    def create_send_job(self, job: SendJob) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO send_jobs
                (job_id, room_id, content, scheduled_at, status, retry_count, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.room_id,
                    job.content,
                    job.scheduled_at.isoformat(),
                    job.status,
                    job.retry_count,
                    job.last_error,
                ),
            )

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            result: dict[str, int] = {}
            for table in ("raw_messages", "feedback_items", "send_jobs"):
                result[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            return result

    def list_jobs(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT job_id, room_id, content, scheduled_at, status, retry_count, last_error "
                "FROM send_jobs ORDER BY scheduled_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_due_jobs(self, limit: int = 10) -> list[SendJob]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        stale_before = (now_dt - timedelta(minutes=5)).isoformat()
        with self.connect() as conn:
            conn.execute(
                "UPDATE send_jobs SET status='pending', claimed_at=NULL, "
                "last_error='程序中断，任务已自动恢复' "
                "WHERE status='claimed' AND claimed_at <= ?",
                (stale_before,),
            )
            rows = conn.execute(
                "SELECT * FROM send_jobs WHERE status = 'pending' AND scheduled_at <= ? "
                "ORDER BY scheduled_at LIMIT ?",
                (now, limit),
            ).fetchall()
            jobs: list[SendJob] = []
            for row in rows:
                conn.execute(
                    "UPDATE send_jobs SET status='claimed', claimed_at=? WHERE job_id=? AND status='pending'",
                    (now, row["job_id"]),
                )
                jobs.append(
                    SendJob(
                        job_id=row["job_id"],
                        room_id=row["room_id"],
                        content=row["content"],
                        scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
                        status="claimed",
                        retry_count=row["retry_count"],
                        last_error=row["last_error"],
                    )
                )
            return jobs

    def finish_job(self, job_id: str, success: bool, error: str = "") -> None:
        with self.connect() as conn:
            if success:
                conn.execute("UPDATE send_jobs SET status='sent', last_error='' WHERE job_id=?", (job_id,))
            else:
                retry_delay = min(30, 2 ** min(5, self._job_retry_count(conn, job_id)))
                next_attempt = (datetime.now(timezone.utc) + timedelta(minutes=retry_delay)).isoformat()
                conn.execute(
                    "UPDATE send_jobs SET status='pending', retry_count=retry_count+1, "
                    "last_error=?, scheduled_at=?, claimed_at=NULL WHERE job_id=?",
                    (error, next_attempt, job_id),
                )

    @staticmethod
    def _job_retry_count(conn: sqlite3.Connection, job_id: str) -> int:
        row = conn.execute("SELECT retry_count FROM send_jobs WHERE job_id=?", (job_id,)).fetchone()
        return int(row["retry_count"]) if row else 0
