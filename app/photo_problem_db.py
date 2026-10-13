from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings
from app.db import utc_now


@contextmanager
def connect():
    connection = sqlite3.connect(settings.data_dir / "immich-photo-problems.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS photo_problems (
                asset_id TEXT PRIMARY KEY,
                original_file_name TEXT NOT NULL,
                live_photo_video_id TEXT,
                status TEXT NOT NULL,
                error TEXT,
                target_asset_id TEXT,
                manifest TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            UPDATE photo_problems SET status='failed',
            error='Interrupted by restart. Retry to resume using the saved backup.'
            WHERE status IN ('queued', 'repairing')
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(photo_problems)")}
        if "source_server" not in columns:
            connection.execute("ALTER TABLE photo_problems ADD COLUMN source_server TEXT")
        if "live_photo_verified" not in columns:
            connection.execute(
                "ALTER TABLE photo_problems ADD COLUMN live_photo_verified INTEGER NOT NULL DEFAULT 0"
            )


def record(
    asset: dict, status: str, error: str | None = None, *, server: str | None = None
) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO photo_problems
            (asset_id, original_file_name, live_photo_video_id, status, error, updated_at,
             source_server, live_photo_verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status,
                error=excluded.error, updated_at=excluded.updated_at,
                original_file_name=excluded.original_file_name,
                live_photo_video_id=excluded.live_photo_video_id,
                source_server=excluded.source_server,
                live_photo_verified=excluded.live_photo_verified
        """,
            (
                asset["id"],
                asset.get("originalFileName") or asset["id"],
                asset.get("livePhotoVideoId"),
                status,
                error,
                utc_now(),
                server,
                int("livePhotoVideoId" in asset),
            ),
        )


def finish_scan(seen_ids: set[str]) -> None:
    """Drop stale detection rows only after a complete, successful library enumeration."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT asset_id FROM photo_problems WHERE manifest IS NULL "
            "AND status IN ('problem', 'clean', 'scan-error', 'failed')"
        ).fetchall()
        connection.executemany(
            "DELETE FROM photo_problems WHERE asset_id=?",
            [(row[0],) for row in rows if row[0] not in seen_ids],
        )


def update(asset_id: str, **values) -> None:
    allowed = {
        "status", "error", "target_asset_id", "manifest",
        "original_file_name", "live_photo_video_id", "live_photo_verified",
    }
    if not values or not set(values) <= allowed:
        raise ValueError("Invalid photo problem fields")
    values["updated_at"] = utc_now()
    with connect() as connection:
        connection.execute(
            "UPDATE photo_problems SET "
            + ", ".join(f"{key}=?" for key in values)
            + " WHERE asset_id=?",
            [*values.values(), asset_id],
        )


def get(asset_id: str):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM photo_problems WHERE asset_id=?",
            (asset_id,),
        ).fetchone()


def save_manifest(asset_id: str, manifest: dict) -> None:
    serialized = json.dumps(manifest, indent=2)
    directory = Path(manifest["files"]["photo"]["path"]).resolve().parent
    root = (settings.data_dir / "photo-repair" / asset_id).resolve()
    if directory != root:
        raise ValueError("Backup manifest directory is outside the repair directory")
    temporary = directory / "manifest.json.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        file.write(serialized)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(directory / "manifest.json")
    update(asset_id, manifest=serialized, target_asset_id=manifest.get("target_id"))


def list_problems(limit: int = 25, offset: int = 0, status: str = ""):
    where = "status=?" if status else "status!='clean'"
    params = [status] if status else []
    with connect() as connection:
        return list(
            connection.execute(
                f"SELECT * FROM photo_problems WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        )


def count(status: str = "") -> int:
    with connect() as connection:
        if status:
            return connection.execute(
                "SELECT COUNT(*) FROM photo_problems WHERE status=?",
                (status,),
            ).fetchone()[0]
        return connection.execute(
            "SELECT COUNT(*) FROM photo_problems WHERE status!='clean'",
        ).fetchone()[0]


def repairable_ids() -> list[str]:
    with connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT asset_id FROM photo_problems WHERE status='problem'",
            )
        ]


def clear_scan_cache() -> None:
    # Never discard recovery state or records of completed replacements.
    with connect() as connection:
        connection.execute("""
            DELETE FROM photo_problems
            WHERE manifest IS NULL AND status IN ('clean', 'problem', 'scan-error', 'failed')
        """)
