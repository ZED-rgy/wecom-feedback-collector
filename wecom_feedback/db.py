from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import FeedbackItem, RawMessage, SendJob


MAX_AUTOMATIC_RETRIES = 3


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
                    target_group_name TEXT NOT NULL DEFAULT '',
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
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(send_jobs)")}
            if "target_group_name" not in columns:
                conn.execute(
                    "ALTER TABLE send_jobs ADD COLUMN target_group_name TEXT NOT NULL DEFAULT ''"
                )
            # Jobs created by versions before target snapshots were introduced
            # cannot be safely routed after a restart or group switch.
            conn.execute(
                "UPDATE send_jobs SET status='cancelled', "
                "last_error='历史任务缺少目标群快照，已取消，请重新生成摘要', claimed_at=NULL "
                "WHERE status IN ('pending','claimed') AND target_group_name=''"
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

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sync_state WHERE state_key=?", (key,))

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

    def get_feedback(self, feedback_id: str) -> FeedbackItem | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM feedback_items WHERE feedback_id=?", (feedback_id,)).fetchone()
        return self._feedback_from_row(row) if row else None

    def feedback_by_ids(self, feedback_ids: list[str]) -> list[FeedbackItem]:
        if not feedback_ids:
            return []
        placeholders = ",".join("?" for _ in feedback_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM feedback_items WHERE feedback_id IN ({placeholders}) ORDER BY created_at DESC",
                feedback_ids,
            ).fetchall()
        return [self._feedback_from_row(row) for row in rows]

    def update_feedback(self, feedback_id: str, values: dict[str, object]) -> FeedbackItem | None:
        allowed = {"title", "description", "feedback_type", "priority", "status", "need_more_info"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get_feedback(feedback_id)
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE feedback_items SET {assignments} WHERE feedback_id=?",
                [*updates.values(), feedback_id],
            )
        return self.get_feedback(feedback_id)

    @staticmethod
    def _feedback_from_row(row: sqlite3.Row) -> FeedbackItem:
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

    def list_messages(self, room_id: str, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT message_id, sender_name, message_type, content, mentioned_account, created_at, payload_json "
                "FROM raw_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            result.append(
                {
                    "message_id": row["message_id"],
                    "sender_name": row["sender_name"],
                    "message_type": row["message_type"],
                    "content": row["content"],
                    "mentioned_account": bool(row["mentioned_account"]),
                    "created_at": row["created_at"],
                    "source": "本地数据库" if row["message_id"].startswith("local-") else payload.get("source", "界面识别"),
                }
            )
        return result

    def smart_table_sync_counts(self, room_id: str) -> dict[str, int]:
        with self.connect() as conn:
            total = int(
                conn.execute("SELECT COUNT(*) AS n FROM feedback_items WHERE room_id=?", (room_id,)).fetchone()["n"]
            )
            synced = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM feedback_items f JOIN sync_state s "
                    "ON s.state_key='smart_table_synced:' || f.feedback_id "
                    "WHERE f.room_id=? AND s.state_value='1'",
                    (room_id,),
                ).fetchone()["n"]
            )
        return {"total": total, "synced": synced, "pending": max(0, total - synced)}

    def create_send_job(self, job: SendJob) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO send_jobs
                (job_id, room_id, target_group_name, content, scheduled_at, status, retry_count, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.room_id,
                    job.target_group_name,
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
                "SELECT job_id, room_id, target_group_name, content, scheduled_at, status, retry_count, last_error "
                "FROM send_jobs ORDER BY scheduled_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> SendJob | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM send_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return SendJob(
            job_id=row["job_id"], room_id=row["room_id"], target_group_name=row["target_group_name"], content=row["content"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]), status=row["status"],
            retry_count=row["retry_count"], last_error=row["last_error"],
        )

    def cancel_job(self, job_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE send_jobs SET status='cancelled', last_error='', claimed_at=NULL "
                "WHERE job_id=? AND status IN ('pending','claimed')",
                (job_id,),
            )
            return cursor.rowcount == 1

    def retry_job(self, job_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE send_jobs SET status='pending', retry_count=0, scheduled_at=?, last_error='', claimed_at=NULL "
                "WHERE job_id=? AND status IN ('pending','failed','cancelled','unconfirmed')",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            return cursor.rowcount == 1

    def mark_job_unconfirmed(self, job_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE send_jobs SET status='unconfirmed', last_error=?, claimed_at=NULL WHERE job_id=?",
                (error, job_id),
            )

    def cancel_claimed_job(self, job_id: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE send_jobs SET status='cancelled', last_error=?, claimed_at=NULL "
                "WHERE job_id=? AND status='claimed'",
                (reason, job_id),
            )

    def cancel_jobs_for_target(self, room_id: str, group_name: str, reason: str) -> int:
        """Cancel queued work before switching the monitored group."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE send_jobs SET status='cancelled', last_error=?, claimed_at=NULL "
                "WHERE status IN ('pending','claimed') AND "
                "(room_id=? OR target_group_name=? OR target_group_name='')",
                (reason, room_id, group_name),
            )
            return int(cursor.rowcount)

    def find_active_job(
        self, room_id: str, group_name: str, content: str, since: datetime
    ) -> SendJob | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM send_jobs WHERE room_id=? AND target_group_name=? "
                "AND content=? AND status IN ('pending','claimed') AND scheduled_at>=? "
                "ORDER BY scheduled_at DESC LIMIT 1",
                (room_id, group_name, content, since.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return SendJob(
            job_id=row["job_id"], room_id=row["room_id"], target_group_name=row["target_group_name"],
            content=row["content"], scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            status=row["status"], retry_count=row["retry_count"], last_error=row["last_error"],
        )

    def claim_job(self, job_id: str) -> SendJob | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM send_jobs WHERE job_id=? AND status='pending'", (job_id,)).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE send_jobs SET status='claimed', claimed_at=? WHERE job_id=? AND status='pending'",
                (now, job_id),
            )
            if cursor.rowcount != 1:
                return None
        return SendJob(
            job_id=row["job_id"], room_id=row["room_id"], target_group_name=row["target_group_name"], content=row["content"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]), status="claimed",
            retry_count=row["retry_count"], last_error=row["last_error"],
        )

    def claim_due_jobs(
        self, limit: int = 10, room_id: str | None = None, group_name: str | None = None
    ) -> list[SendJob]:
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
            filters = ["status = 'pending'", "scheduled_at <= ?"]
            parameters: list[object] = [now]
            if room_id is not None:
                filters.append("room_id = ?")
                parameters.append(room_id)
            if group_name is not None:
                filters.append("target_group_name = ?")
                parameters.append(group_name)
            parameters.append(limit)
            rows = conn.execute(
                f"SELECT * FROM send_jobs WHERE {' AND '.join(filters)} "
                "ORDER BY scheduled_at LIMIT ?",
                parameters,
            ).fetchall()
            jobs: list[SendJob] = []
            for row in rows:
                cursor = conn.execute(
                    "UPDATE send_jobs SET status='claimed', claimed_at=? WHERE job_id=? AND status='pending'",
                    (now, row["job_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                jobs.append(
                    SendJob(
                        job_id=row["job_id"],
                        room_id=row["room_id"],
                        target_group_name=row["target_group_name"],
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
                retry_count = self._job_retry_count(conn, job_id)
                if retry_count >= MAX_AUTOMATIC_RETRIES:
                    conn.execute(
                        "UPDATE send_jobs SET status='failed', last_error=?, claimed_at=NULL WHERE job_id=?",
                        (f"自动重试已达上限（{MAX_AUTOMATIC_RETRIES} 次）：{error}", job_id),
                    )
                else:
                    retry_delay = min(30, 2 ** retry_count)
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
