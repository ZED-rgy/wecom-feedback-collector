from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ENV_FILE = Path(".env")


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load simple KEY=VALUE pairs without adding a third-party dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


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
    poll_interval_seconds: int
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
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
            poll_interval_seconds=int(os.getenv("WECOM_POLL_INTERVAL_SECONDS", "10")),
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

    def public_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "archive_enabled": self.archive_enabled,
            "archive_corp_id": self.archive_corp_id,
            "archive_secret_configured": bool(self.archive_secret),
            "archive_private_key_path": self.archive_private_key_path,
            "target_room_id": self.target_room_id,
            "target_group_name": self.target_group_name,
            "target_group_remark": self.target_group_remark,
            "target_account_id": self.target_account_id,
            "target_account_names": ", ".join(self.target_account_names),
            "context_window_seconds": self.context_window_seconds,
            "summary_times": ", ".join(self.summary_times),
            "poll_interval_seconds": self.poll_interval_seconds,
            "dry_run": self.dry_run,
        }


def save_env(values: dict[str, object], path: Path = ENV_FILE) -> None:
    """Persist dashboard-editable values; blank secrets leave existing secrets unchanged."""
    load_dotenv(path)
    mapping = {
        "WECOM_DATABASE_PATH": values.get("database_path", "data/feedback.db"),
        "WECOM_ARCHIVE_ENABLED": str(values.get("archive_enabled", False)).lower(),
        "WECOM_ARCHIVE_CORP_ID": values.get("archive_corp_id", ""),
        "WECOM_ARCHIVE_PRIVATE_KEY_PATH": values.get("archive_private_key_path", ""),
        "WECOM_TARGET_ROOM_ID": values.get("target_room_id", ""),
        "WECOM_TARGET_GROUP_NAME": values.get("target_group_name", ""),
        "WECOM_TARGET_GROUP_REMARK": values.get("target_group_remark", ""),
        "WECOM_TARGET_ACCOUNT_ID": values.get("target_account_id", ""),
        "WECOM_TARGET_ACCOUNT_NAMES": values.get("target_account_names", ""),
        "WECOM_CONTEXT_WINDOW_SECONDS": values.get("context_window_seconds", 90),
        "WECOM_SUMMARY_TIMES": values.get("summary_times", "12:00,18:00"),
        "WECOM_POLL_INTERVAL_SECONDS": values.get("poll_interval_seconds", 10),
        "WECOM_DRY_RUN": str(values.get("dry_run", True)).lower(),
    }
    secret = str(values.get("archive_secret", "")).strip()
    if secret:
        mapping["WECOM_ARCHIVE_SECRET"] = secret
    elif os.getenv("WECOM_ARCHIVE_SECRET"):
        mapping["WECOM_ARCHIVE_SECRET"] = os.environ["WECOM_ARCHIVE_SECRET"]
    lines = ["# Managed by the local WeCom feedback dashboard", ""]
    lines.extend(f"{key}={str(value).strip()}" for key, value in mapping.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in mapping.items():
        os.environ[key] = str(value)
