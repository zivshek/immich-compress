from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from app import db
from app.config import effective_settings


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
                          queued_at TEXT,
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
                db.update_job("original-id", queued_at="2020-01-01T00:00:00+00:00")
                db.upsert_job("original-id", "video.mp4", "compressing")
                stable_queued_at = db.get_job("original-id")["queued_at"]
                db.upsert_job("pending-id", "pending.mp4")
                db.set_setting("immich_url", "http://immich:2283")
                db.set_setting("immich_api_key", "secret")
                db.set_setting("max_concurrent_jobs", "3")
                db.set_setting("upscale_to_4k", "true")
                db.set_setting("video_taken_before", "2026-06-01T12:00:00.000Z")
                db.set_setting("compression_mode", "perceptual-av1")
                db.set_setting("video_score", "93")
                db.set_setting("min_savings_percent", "20")
                configured = effective_settings()

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
                self.assertIn("queued_at", columns)
                self.assertIsNone(batch_table)
                self.assertEqual(db.get_job_for_asset("copied-id")["asset_id"], "original-id")
                self.assertEqual(stable_queued_at, "2020-01-01T00:00:00+00:00")
                self.assertEqual(db.list_jobs(2)[0]["asset_id"], "pending-id")
                self.assertEqual(configured.immich_url, "http://immich:2283")
                self.assertEqual(configured.immich_api_key, "secret")
                self.assertEqual(configured.max_concurrent_jobs, 3)
                self.assertTrue(configured.upscale_to_4k)
                self.assertEqual(configured.video_taken_before, "2026-06-01T12:00:00.000Z")
                self.assertEqual(configured.compression_mode, "perceptual-av1")
                self.assertEqual(configured.video_score, 93)
                self.assertEqual(configured.min_savings_percent, 20)

                marked = db.mark_asset_as_processed(
                    {
                        "id": "external-id",
                        "originalFileName": "external.mp4",
                        "originalFileSize": 123456,
                    }
                )
                external_job = db.get_job("external-id")
                self.assertTrue(marked)
                self.assertEqual(external_job["state"], "processed")
                self.assertEqual(external_job["original_size"], 123456)
                self.assertEqual(external_job["compressed_size"], 123456)
                self.assertEqual(external_job["saved_bytes"], 0)
                self.assertFalse(
                    db.mark_asset_as_processed(
                        {
                            "id": "external-id",
                            "originalFileName": "external.mp4",
                            "originalFileSize": 654321,
                        }
                    )
                )
                self.assertEqual(db.get_job("external-id")["original_size"], 123456)

                db.upsert_job("canceled-id", "canceled.mp4", "canceled")
                db.update_job("canceled-id", logs="Canceled and cleaned up.")
                self.assertTrue(
                    db.mark_asset_as_processed(
                        {
                            "id": "canceled-id",
                            "originalFileName": "canceled.mp4",
                            "originalFileSize": 222222,
                        }
                    )
                )
                canceled_job = db.get_job("canceled-id")
                self.assertEqual(canceled_job["state"], "processed")
                self.assertEqual(canceled_job["compressed_size"], 222222)
                self.assertIn("Canceled and cleaned up.", canceled_job["logs"])
                self.assertIn("Marked as already processed", canceled_job["logs"])
            finally:
                db.settings = original_settings


if __name__ == "__main__":
    unittest.main()
