from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS compression_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              asset_id TEXT NOT NULL,
              target_asset_id TEXT,
              original_file_name TEXT NOT NULL,
              original_path TEXT,
              output_path TEXT,
              state TEXT NOT NULL,
              original_size INTEGER,
              compressed_size INTEGER,
              saved_bytes INTEGER,
              progress_stage TEXT,
              progress_percent REAL,
              error TEXT,
              logs TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              process_started_at TEXT
            )
            """
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_asset_id ON compression_jobs(asset_id)"
        )
        ensure_column(db, "compression_jobs", "target_asset_id", "TEXT")
        ensure_column(db, "compression_jobs", "progress_stage", "TEXT")
        ensure_column(db, "compression_jobs", "progress_percent", "REAL")
        ensure_column(db, "compression_jobs", "process_started_at", "TEXT")
        drop_column(db, "compression_jobs", "updated_at")
        drop_column(db, "compression_jobs", "batch_id")
        drop_column(db, "app_settings", "updated_at")
        db.execute("DROP TABLE IF EXISTS processing_batches")
        db.execute(
            """
            UPDATE compression_jobs
            SET process_started_at = created_at
            WHERE process_started_at IS NULL
            """
        )
        repair_processed_metrics(db)


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def drop_column(db: sqlite3.Connection, table: str, column: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def repair_processed_metrics(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT asset_id, logs
        FROM compression_jobs
        WHERE state = 'processed'
          AND (saved_bytes IS NULL OR saved_bytes = 0)
          AND logs LIKE '%Compressed to % MB; saved % MB%'
        """
    ).fetchall()
    pattern = re.compile(r"Compressed to ([\d.]+) MB; saved ([\d.-]+) MB")
    for row in rows:
        match = pattern.search(row["logs"] or "")
        if not match:
            continue
        compressed_size = round(float(match.group(1)) * 1024 * 1024)
        saved_bytes = round(float(match.group(2)) * 1024 * 1024)
        if saved_bytes <= 0:
            continue
        original_size = compressed_size + saved_bytes
        db.execute(
            """
            UPDATE compression_jobs
            SET original_size = ?, compressed_size = ?, saved_bytes = ?
            WHERE asset_id = ?
            """,
            (original_size, compressed_size, saved_bytes, row["asset_id"]),
        )


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path or settings.database_path)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def list_jobs(limit: int = 100) -> list[sqlite3.Row]:
    with connect() as db:
        return list(
            db.execute(
                """
                SELECT * FROM compression_jobs
                ORDER BY process_started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def list_jobs_by_states(states: set[str]) -> list[sqlite3.Row]:
    if not states:
        return []
    placeholders = ",".join("?" for _ in states)
    with connect() as db:
        return list(
            db.execute(
                f"SELECT * FROM compression_jobs WHERE state IN ({placeholders})",
                list(states),
            )
        )


def list_jobs_for_assets(asset_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM compression_jobs
            WHERE asset_id IN ({placeholders})
               OR target_asset_id IN ({placeholders})
            """,
            [*asset_ids, *asset_ids],
        ).fetchall()
        result = {}
        for row in rows:
            result[row["asset_id"]] = row
            if row["target_asset_id"]:
                result[row["target_asset_id"]] = row
        return result


def get_dashboard_stats() -> sqlite3.Row:
    with connect() as db:
        return db.execute(
            """
            SELECT
              COUNT(*) AS total_jobs,
              SUM(CASE WHEN state IN ('processed', 'copied', 'copied-and-trashed', 'replaced') THEN 1 ELSE 0 END)
                AS converted_jobs,
              SUM(CASE WHEN state = 'review' THEN 1 ELSE 0 END) AS review_jobs,
              SUM(CASE WHEN state IN ('failed', 'copy-failed', 'trash-failed', 'replace-failed') THEN 1 ELSE 0 END)
                AS failed_jobs,
              COALESCE(SUM(CASE
                WHEN state IN ('processed', 'copied', 'copied-and-trashed', 'replaced')
                THEN original_size ELSE 0 END), 0) AS converted_original_bytes,
              COALESCE(SUM(CASE
                WHEN state IN ('processed', 'copied', 'copied-and-trashed', 'replaced')
                THEN compressed_size ELSE 0 END), 0) AS converted_compressed_bytes,
              COALESCE(SUM(CASE
                WHEN state IN ('processed', 'copied', 'copied-and-trashed', 'replaced')
                THEN saved_bytes ELSE 0 END), 0) AS converted_saved_bytes
            FROM compression_jobs
            """
        ).fetchone()


def get_job(asset_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM compression_jobs WHERE asset_id = ?", (asset_id,)).fetchone()


def get_job_for_asset(asset_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            """
            SELECT * FROM compression_jobs
            WHERE asset_id = ? OR target_asset_id = ?
            LIMIT 1
            """,
            (asset_id, asset_id),
        ).fetchone()


def upsert_job(
    asset_id: str,
    original_file_name: str,
    state: str = "pending",
) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO compression_jobs(asset_id, original_file_name, state, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              original_file_name = excluded.original_file_name,
              state = excluded.state
            """,
            (asset_id, original_file_name, state, now),
        )


def update_job(asset_id: str, **values: object) -> None:
    if not values:
        return
    columns = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [asset_id]
    with connect() as db:
        db.execute(f"UPDATE compression_jobs SET {columns} WHERE asset_id = ?", params)


def append_job_log(asset_id: str, line: str, *, max_chars: int = 12000) -> None:
    job = get_job(asset_id)
    current = job["logs"] if job else ""
    text = (current + "\n" + line).strip()
    if len(text) > max_chars:
        text = text[-max_chars:]
    update_job(asset_id, logs=text)


def get_setting(key: str, default: str = "") -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO app_settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
