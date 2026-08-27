from __future__ import annotations

import os
import shutil
from pathlib import Path


APP_NAME = "WeComFeedbackCollector"


def application_home() -> Path:
    """Return the directory containing the executable/source checkout."""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_home() -> Path:
    """Return a writable per-user directory for config, state and logs."""
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def config_path() -> Path:
    return user_data_home() / ".env"


def default_database_path() -> Path:
    return user_data_home() / "data" / "feedback.db"


def ensure_user_data_dirs() -> Path:
    root = user_data_home()
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def migrate_legacy_installation() -> None:
    """Migrate the old portable layout once, without overwriting user data."""
    root = ensure_user_data_dirs()
    target_env = config_path()
    legacy_root = application_home()
    legacy_env = legacy_root / ".env"
    if target_env.exists() or not legacy_env.exists() or target_env == legacy_env:
        return
    shutil.copy2(legacy_env, target_env)
    # Keep the old database usable by recording an absolute path in the new
    # config.  The caller may still choose to copy it below when it exists.
    raw_database = "data/feedback.db"
    for line in legacy_env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("WECOM_DATABASE_PATH="):
            raw_database = line.split("=", 1)[1].strip().strip('"').strip("'") or raw_database
            break
    old_database = Path(raw_database)
    if not old_database.is_absolute():
        old_database = legacy_root / old_database
    new_database = root / "data" / "feedback.db"
    if old_database.exists() and not new_database.exists():
        shutil.copy2(old_database, new_database)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(old_database) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(new_database) + suffix))
    text = target_env.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("WECOM_DATABASE_PATH="):
            lines.append(f"WECOM_DATABASE_PATH={new_database}")
        else:
            lines.append(line)
    temporary = target_env.with_name(f".{target_env.name}.migrate.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target_env)
