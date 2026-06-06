from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
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
              batch_id TEXT,
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_batches (
              id TEXT PRIMARY KEY,
              total_jobs INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "compression_jobs", "batch_id", "TEXT")
        ensure_column(db, "compression_jobs", "target_asset_id", "TEXT")
        ensure_column(db, "compression_jobs", "progress_stage", "TEXT")
        ensure_column(db, "compression_jobs", "progress_percent", "REAL")
        repair_processed_metrics(db)


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def list_jobs_for_assets(asset_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    with connect() as db:
        rows = db.execute(
            f"SELECT * FROM compression_jobs WHERE asset_id IN ({placeholders})",
            asset_ids,
        ).fetchall()
        return {row["asset_id"]: row for row in rows}


def create_batch(total_jobs: int) -> str:
    batch_id = str(uuid4())
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO processing_batches(id, total_jobs, created_at, updated_at)
            VALUES(?, ?, ?, ?)
            """,
            (batch_id, total_jobs, now, now),
        )
    return batch_id


def latest_batch() -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            """
            SELECT * FROM processing_batches
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()


def batch_jobs(batch_id: str) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute(
            """
            SELECT * FROM compression_jobs
            WHERE batch_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (batch_id,),
        ).fetchall()


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


def batch_stats(batch_id: str | None) -> dict[str, object] | None:
    if not batch_id:
        batch = latest_batch()
        if not batch:
            return None
        batch_id = batch["id"]
    else:
        batch = None

    jobs = batch_jobs(batch_id)
    if not batch and not jobs:
        return None
    completed_states = {
        "processed",
        "review",
        "copied",
        "copied-and-trashed",
        "rejected",
        "failed",
        "copy-failed",
        "trash-failed",
    }
    completed = sum(1 for job in jobs if job["state"] in completed_states)
    total = len(jobs) or (batch["total_jobs"] if batch else 0)
    active = [job for job in jobs if job["state"] not in completed_states]
    if total:
        percent = (completed / total) * 100
    else:
        percent = 0
    return {
        "id": batch_id,
        "jobs": jobs,
        "total": total,
        "completed": completed,
        "active": active,
        "percent": percent,
    }


def get_job(asset_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM compression_jobs WHERE asset_id = ?", (asset_id,)).fetchone()


def upsert_job(
    asset_id: str,
    original_file_name: str,
    state: str = "pending",
    batch_id: str | None = None,
) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO compression_jobs(asset_id, original_file_name, state, batch_id, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              original_file_name = excluded.original_file_name,
              state = excluded.state,
              batch_id = COALESCE(excluded.batch_id, compression_jobs.batch_id),
              updated_at = excluded.updated_at
            """,
            (asset_id, original_file_name, state, batch_id, now, now),
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
