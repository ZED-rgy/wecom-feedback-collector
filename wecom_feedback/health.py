from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Settings
from .db import Database


@dataclass(frozen=True)
class HealthSnapshot:
    ok: bool
    database_path: str
    archive_enabled: bool
    dry_run: bool
    target_room_configured: bool
    target_account_configured: bool
    missing_config: tuple[str, ...]
    counts: dict[str, int]

    def as_dict(self) -> dict:
        return asdict(self)


def check_health(settings: Settings, database: Database) -> HealthSnapshot:
    missing = tuple(settings.missing_required())
    return HealthSnapshot(
        ok=not missing,
        database_path=str(settings.database_path),
        archive_enabled=settings.archive_enabled,
        dry_run=settings.dry_run,
        target_room_configured=bool(settings.target_room_id or settings.target_group_name),
        target_account_configured=bool(settings.target_account_id or settings.target_account_names),
        missing_config=missing,
        counts=database.counts(),
    )
