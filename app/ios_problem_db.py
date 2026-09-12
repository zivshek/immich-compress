from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings
from app.db import utc_now
from app.ios_repair import IosCompatibilityAnalysis, MediaProbe


SQLITE_BUSY_TIMEOUT_MS = 30000


def database_path() -> Path:
    return settings.data_dir / "immich-ios-problems.sqlite"


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect(enable_wal=True) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ios_problem_videos (
              asset_id TEXT PRIMARY KEY,
              original_file_name TEXT NOT NULL,
              original_file_size INTEGER,
              local_date_time TEXT,
              duration TEXT,
              width INTEGER,
              height INTEGER,
              format_name TEXT,
              video_codec TEXT,
              video_profile TEXT,
              pixel_format TEXT,
              color_primaries TEXT,
              color_transfer TEXT,
              color_space TEXT,
              audio_codecs TEXT NOT NULL DEFAULT '[]',
              handler_name TEXT,
              reasons TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'problem',
              error TEXT,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              scanned_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ios_problem_status ON ios_problem_videos(status)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ios_problem_scanned_at ON ios_problem_videos(scanned_at)"
        )


@contextmanager
def connect(path: Path | None = None, *, enable_wal: bool = False) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path or database_path(), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if enable_wal:
        db.execute("PRAGMA journal_mode = WAL")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def upsert_problem(
    asset: dict,
    probe: MediaProbe,
    analysis: IosCompatibilityAnalysis,
) -> None:
    now = utc_now()
    exif = asset.get("exifInfo") or {}
    original_file_size = (
        asset.get("originalFileSize")
        or asset.get("fileSizeInByte")
        or exif.get("fileSizeInByte")
        or exif.get("fileSize")
    )
    with connect() as db:
        db.execute(
            """
            INSERT INTO ios_problem_videos(
              asset_id,
              original_file_name,
              original_file_size,
              local_date_time,
              duration,
              width,
              height,
              format_name,
              video_codec,
              video_profile,
              pixel_format,
              color_primaries,
              color_transfer,
              color_space,
              audio_codecs,
              handler_name,
              reasons,
              status,
              error,
              first_seen_at,
              last_seen_at,
              scanned_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'problem', NULL, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              original_file_name = excluded.original_file_name,
              original_file_size = excluded.original_file_size,
              local_date_time = excluded.local_date_time,
              duration = excluded.duration,
              width = excluded.width,
              height = excluded.height,
              format_name = excluded.format_name,
              video_codec = excluded.video_codec,
              video_profile = excluded.video_profile,
              pixel_format = excluded.pixel_format,
              color_primaries = excluded.color_primaries,
              color_transfer = excluded.color_transfer,
              color_space = excluded.color_space,
              audio_codecs = excluded.audio_codecs,
              handler_name = excluded.handler_name,
              reasons = excluded.reasons,
              status = CASE
                WHEN ios_problem_videos.status IN ('queued', 'repairing', 'review') THEN ios_problem_videos.status
                ELSE 'problem'
              END,
              error = NULL,
              last_seen_at = excluded.last_seen_at,
              scanned_at = excluded.scanned_at
            """,
            (
                asset["id"],
                asset.get("originalFileName") or asset["id"],
                original_file_size,
                asset.get("localDateTime") or asset.get("fileCreatedAt"),
                asset.get("duration"),
                probe.width,
                probe.height,
                probe.format_name,
                probe.video_codec,
                probe.video_profile,
                probe.pixel_format,
                probe.color_primaries,
                probe.color_transfer,
                probe.color_space,
                json.dumps(probe.audio_codecs),
                probe.handler_name,
                json.dumps(analysis.reasons),
                now,
                now,
                now,
            ),
        )


def mark_not_problem(asset_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM ios_problem_videos WHERE asset_id = ?", (asset_id,))


def has_row(asset_id: str) -> bool:
    with connect() as db:
        row = db.execute(
            "SELECT 1 FROM ios_problem_videos WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        return row is not None


def clear_all() -> None:
    with connect() as db:
        db.execute("DELETE FROM ios_problem_videos")


def upsert_scan_error(asset: dict, error: str) -> None:
    now = utc_now()
    exif = asset.get("exifInfo") or {}
    original_file_size = (
        asset.get("originalFileSize")
        or asset.get("fileSizeInByte")
        or exif.get("fileSizeInByte")
        or exif.get("fileSize")
    )
    with connect() as db:
        db.execute(
            """
            INSERT INTO ios_problem_videos(
              asset_id,
              original_file_name,
              original_file_size,
              local_date_time,
              duration,
              reasons,
              status,
              error,
              first_seen_at,
              last_seen_at,
              scanned_at
            )
            VALUES(?, ?, ?, ?, ?, '[]', 'scan-error', ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              original_file_name = excluded.original_file_name,
              original_file_size = excluded.original_file_size,
              local_date_time = excluded.local_date_time,
              duration = excluded.duration,
              status = 'scan-error',
              error = excluded.error,
              last_seen_at = excluded.last_seen_at,
              scanned_at = excluded.scanned_at
            """,
            (
                asset["id"],
                asset.get("originalFileName") or asset["id"],
                original_file_size,
                asset.get("localDateTime") or asset.get("fileCreatedAt"),
                asset.get("duration"),
                error,
                now,
                now,
                now,
            ),
        )


def update_status(asset_id: str, status: str, error: str | None = None) -> None:
    with connect() as db:
        db.execute(
            """
            UPDATE ios_problem_videos
            SET status = ?, error = ?, last_seen_at = ?
            WHERE asset_id = ?
            """,
            (status, error, utc_now(), asset_id),
        )


def list_statuses(asset_ids: list[str]) -> dict[str, str]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    with connect() as db:
        rows = db.execute(
            f"SELECT asset_id, status FROM ios_problem_videos WHERE asset_id IN ({placeholders})",
            asset_ids,
        )
        return {row["asset_id"]: row["status"] for row in rows}


def list_repairable_asset_ids() -> list[str]:
    with connect() as db:
        return [
            row["asset_id"]
            for row in db.execute(
                "SELECT asset_id FROM ios_problem_videos WHERE status = 'problem'"
            )
        ]


def count_problems(status: str = "") -> int:
    with connect() as db:
        if status:
            row = db.execute(
                "SELECT COUNT(*) FROM ios_problem_videos WHERE status = ?",
                (status,),
            ).fetchone()
        else:
            row = db.execute("SELECT COUNT(*) FROM ios_problem_videos").fetchone()
        return int(row[0])


def list_problem_statuses() -> list[str]:
    with connect() as db:
        return [
            row["status"]
            for row in db.execute(
                "SELECT DISTINCT status FROM ios_problem_videos ORDER BY status"
            )
        ]


def list_problems(limit: int = 100, offset: int = 0, status: str = "") -> list[sqlite3.Row]:
    with connect() as db:
        where = "WHERE status = ?" if status else ""
        params: list[object] = [status] if status else []
        return list(
            db.execute(
                f"""
                SELECT * FROM ios_problem_videos
                {where}
                ORDER BY scanned_at DESC, original_file_name ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
        )


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]
