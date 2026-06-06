from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from app import db


class DatabaseMigrationTest(unittest.TestCase):
    def test_init_removes_legacy_batch_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_settings = db.settings
            db.settings = replace(db.settings, data_dir=Path(directory))
            try:
                with closing(sqlite3.connect(db.settings.database_path)) as connection:
                    connection.execute(
                        """
                        CREATE TABLE compression_jobs (
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
                          process_started_at TEXT,
                          updated_at TEXT
                        )
                        """
                    )
                    connection.execute(
                        "CREATE TABLE processing_batches (id TEXT PRIMARY KEY, total_jobs INTEGER)"
                    )
                    connection.execute(
                        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    connection.commit()

                db.init_db()
                db.upsert_job("original-id", "video.mp4", "processed")
                db.update_job("original-id", target_asset_id="copied-id")

                with closing(sqlite3.connect(db.settings.database_path)) as connection:
                    columns = {
                        row[1] for row in connection.execute("PRAGMA table_info(compression_jobs)")
                    }
                    batch_table = connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name = 'processing_batches'
                        """
                    ).fetchone()

                self.assertNotIn("batch_id", columns)
                self.assertNotIn("updated_at", columns)
                self.assertIsNone(batch_table)
                self.assertEqual(db.get_job_for_asset("copied-id")["asset_id"], "original-id")
            finally:
                db.settings = original_settings


if __name__ == "__main__":
    unittest.main()
