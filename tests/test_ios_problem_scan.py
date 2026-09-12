from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.ios_problem_scan import IosProblemScanner


class IosProblemScannerTest(unittest.TestCase):
    def test_skips_assets_already_in_problem_db(self) -> None:
        scanner = IosProblemScanner()
        client = MagicMock()
        asset = {"id": "asset-1", "originalFileName": "IMG_4083.MOV"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.ios_problem_scan.ios_problem_db.has_row", return_value=True
        ) as has_row:
            scanner._scan_asset(client, asset, Path(directory))

        has_row.assert_called_once_with("asset-1")
        client.download_original.assert_not_called()
        self.assertEqual(scanner.scanned, 1)
        self.assertEqual(scanner.skipped, 1)
        self.assertEqual(scanner.found, 0)

    def test_probes_assets_missing_from_problem_db(self) -> None:
        scanner = IosProblemScanner()
        client = MagicMock()
        client.config = Settings()
        asset = {"id": "asset-2", "originalFileName": "VID_123.MP4"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.ios_problem_scan.ios_problem_db.has_row", return_value=False
        ) as has_row, patch("app.ios_problem_scan.probe_media") as probe_media, patch(
            "app.ios_problem_scan.analyze_ios_compatibility",
            return_value=MagicMock(needs_repair=True),
        ) as analyze, patch(
            "app.ios_problem_scan.ios_problem_db.upsert_problem"
        ) as upsert_problem:
            scanner._scan_asset(client, asset, Path(directory))

        has_row.assert_called_once_with("asset-2")
        client.download_original.assert_called_once()
        probe_media.assert_called_once()
        analyze.assert_called_once()
        upsert_problem.assert_called_once()
        self.assertEqual(scanner.scanned, 1)
        self.assertEqual(scanner.skipped, 0)
        self.assertEqual(scanner.found, 1)

    def test_run_keeps_existing_problem_rows(self) -> None:
        scanner = IosProblemScanner()
        with tempfile.TemporaryDirectory() as directory:
            scan_settings = Settings(data_dir=Path(directory))
            with patch(
                "app.ios_problem_scan.effective_settings", return_value=scan_settings
            ), patch("app.ios_problem_scan.ImmichClient") as client_cls, patch(
                "app.ios_problem_scan.ios_problem_db.clear_all"
            ) as clear_all:
                client_cls.return_value.search_videos.side_effect = [([], None)]
                scanner._run()

            clear_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
