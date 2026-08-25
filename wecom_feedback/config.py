from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    database_path: Path
    archive_enabled: bool
    archive_corp_id: str
    archive_secret: str
    archive_private_key_path: str
    target_room_id: str
    target_group_name: str
    target_group_remark: str
    target_account_id: str
    target_account_names: tuple[str, ...]
    context_window_seconds: int
    summary_times: tuple[str, ...]
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.getenv("WECOM_DATABASE_PATH", "data/feedback.db")),
            archive_enabled=_bool_env("WECOM_ARCHIVE_ENABLED", False),
            archive_corp_id=os.getenv("WECOM_ARCHIVE_CORP_ID", "").strip(),
            archive_secret=os.getenv("WECOM_ARCHIVE_SECRET", "").strip(),
            archive_private_key_path=os.getenv("WECOM_ARCHIVE_PRIVATE_KEY_PATH", "").strip(),
            target_room_id=os.getenv("WECOM_TARGET_ROOM_ID", "").strip(),
            target_group_name=os.getenv("WECOM_TARGET_GROUP_NAME", "").strip(),
            target_group_remark=os.getenv("WECOM_TARGET_GROUP_REMARK", "").strip(),
            target_account_id=os.getenv("WECOM_TARGET_ACCOUNT_ID", "").strip(),
            target_account_names=_csv_env("WECOM_TARGET_ACCOUNT_NAMES"),
            context_window_seconds=int(os.getenv("WECOM_CONTEXT_WINDOW_SECONDS", "90")),
            summary_times=_csv_env("WECOM_SUMMARY_TIMES") or ("12:00", "18:00"),
            dry_run=_bool_env("WECOM_DRY_RUN", True),
        )

    def missing_required(self) -> list[str]:
        missing: list[str] = []
        if not self.target_room_id:
            missing.append("WECOM_TARGET_ROOM_ID")
        if not self.target_account_id:
            missing.append("WECOM_TARGET_ACCOUNT_ID")
        if not self.target_account_names:
            missing.append("WECOM_TARGET_ACCOUNT_NAMES")
        if self.archive_enabled:
            for field, value in (
                ("WECOM_ARCHIVE_CORP_ID", self.archive_corp_id),
                ("WECOM_ARCHIVE_SECRET", self.archive_secret),
                ("WECOM_ARCHIVE_PRIVATE_KEY_PATH", self.archive_private_key_path),
            ):
                if not value:
                    missing.append(field)
        return missing
