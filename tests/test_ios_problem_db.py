from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app import ios_problem_db
from app.config import settings
from app.ios_repair import IosCompatibilityAnalysis, MediaProbe


class IosProblemDatabaseTest(unittest.TestCase):
    def test_caches_problem_probe_metadata_and_can_clear_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_settings = ios_problem_db.settings
            ios_problem_db.settings = replace(settings, data_dir=Path(directory))
            try:
                ios_problem_db.init_db()
                with ios_problem_db.connect() as connection:
                    busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                probe = MediaProbe(
                    format_name="mov,mp4,m4a,3gp,3g2,mj2",
                    video_codec="vp9",
                    video_profile="Profile 2",
                    pixel_format="yuv420p10le",
                    width=1080,
                    height=1920,
                    rotation=0,
                    color_primaries="bt2020",
                    color_transfer="arib-std-b67",
                    color_space="bt2020nc",
                    audio_codecs=("aac",),
                    handler_name="ISO Media file produced by Google Inc.",
                )
                analysis = IosCompatibilityAnalysis(
                    True,
                    ("VP9 Profile 2 yuv420p10le BT.2020/HLG HDR is unreliable on iOS",),
                )

                ios_problem_db.upsert_problem(
                    {
                        "id": "asset-1",
                        "originalFileName": "IMG_4083.MOV",
                        "originalFileSize": 1234,
                        "localDateTime": "2025-06-15T15:01:40",
                        "duration": "0:00:36.220000",
                    },
                    probe,
                    analysis,
                )

                rows = ios_problem_db.list_problems()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["asset_id"], "asset-1")
                self.assertEqual(busy_timeout, 30000)
                self.assertEqual(rows[0]["video_codec"], "vp9")
                self.assertEqual(rows[0]["status"], "problem")
                self.assertEqual(
                    ios_problem_db.parse_json_list(rows[0]["reasons"]),
                    ["VP9 Profile 2 yuv420p10le BT.2020/HLG HDR is unreliable on iOS"],
                )

                ios_problem_db.mark_not_problem("asset-1")
                self.assertEqual(ios_problem_db.count_problems(), 0)
            finally:
                ios_problem_db.settings = original_settings

    def test_has_row_and_clear_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_settings = ios_problem_db.settings
            ios_problem_db.settings = replace(settings, data_dir=Path(directory))
            try:
                ios_problem_db.init_db()
                ios_problem_db.upsert_problem(
                    {"id": "asset-1", "originalFileName": "IMG_4083.MOV"},
                    MediaProbe(
                        format_name="mov,mp4,m4a,3gp,3g2,mj2",
                        video_codec="vp9",
                        video_profile="Profile 2",
                        pixel_format="yuv420p10le",
                        width=1080,
                        height=1920,
                        rotation=0,
                        color_primaries="bt2020",
                        color_transfer="arib-std-b67",
                        color_space="bt2020nc",
                        audio_codecs=("aac",),
                        handler_name=None,
                    ),
                    IosCompatibilityAnalysis(True, ("unreliable on iOS",)),
                )

                self.assertTrue(ios_problem_db.has_row("asset-1"))
                self.assertFalse(ios_problem_db.has_row("missing"))

                ios_problem_db.clear_all()

                self.assertEqual(ios_problem_db.count_problems(), 0)
                self.assertFalse(ios_problem_db.has_row("asset-1"))
            finally:
                ios_problem_db.settings = original_settings


if __name__ == "__main__":
    unittest.main()
