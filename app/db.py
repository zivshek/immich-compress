from __future__ import annotations

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
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
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
              updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_asset_id ON compression_jobs(asset_id)"
        )
        ensure_column(db, "compression_jobs", "target_asset_id", "TEXT")
        ensure_column(db, "compression_jobs", "progress_stage", "TEXT")
        ensure_column(db, "compression_jobs", "progress_percent", "REAL")


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


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
                WHEN state IN ('copied', 'copied-and-trashed', 'replaced')
                THEN original_size ELSE 0 END), 0) AS converted_original_bytes,
              COALESCE(SUM(CASE
                WHEN state IN ('copied', 'copied-and-trashed', 'replaced')
                THEN compressed_size ELSE 0 END), 0) AS converted_compressed_bytes,
              COALESCE(SUM(CASE
                WHEN state IN ('copied', 'copied-and-trashed', 'replaced')
                THEN saved_bytes ELSE 0 END), 0) AS converted_saved_bytes
            FROM compression_jobs
            """
        ).fetchone()


def get_job(asset_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM compression_jobs WHERE asset_id = ?", (asset_id,)).fetchone()


def upsert_job(asset_id: str, original_file_name: str, state: str = "pending") -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO compression_jobs(asset_id, original_file_name, state, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              original_file_name = excluded.original_file_name,
              state = excluded.state,
              updated_at = excluded.updated_at
            """,
            (asset_id, original_file_name, state, now, now),
        )


def update_job(asset_id: str, **values: object) -> None:
    if not values:
        return
    values["updated_at"] = utc_now()
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
            INSERT INTO app_settings(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, utc_now()),
        )
