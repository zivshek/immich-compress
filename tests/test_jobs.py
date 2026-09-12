from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.jobs import IOS_REPAIR_JOB_KIND, process_asset


class JobsTest(unittest.TestCase):
    def test_missing_asset_fails_ios_repair_job_and_problem_row(self) -> None:
        client = MagicMock()
        client.find_asset_by_id.side_effect = RuntimeError("404 Client Error")

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.jobs.effective_settings", return_value=Settings(data_dir=Path(directory))
        ), patch("app.jobs.ImmichClient", return_value=client), patch(
            "app.jobs.db.get_job", return_value={"job_kind": IOS_REPAIR_JOB_KIND}
        ), patch("app.jobs.db.update_job") as update_job, patch(
            "app.jobs.ios_problem_db.update_status"
        ) as update_status:
            process_asset("asset-1", threading.Event())

        update_job.assert_called_once_with(
            "asset-1",
            state="failed",
            error="Asset no longer exists in Immich: 404 Client Error",
        )
        update_status.assert_called_once_with(
            "asset-1",
            "failed",
            "Asset no longer exists in Immich: 404 Client Error",
        )

    def test_missing_asset_fails_compression_job_without_problem_update(self) -> None:
        client = MagicMock()
        client.find_asset_by_id.side_effect = RuntimeError("404 Client Error")

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.jobs.effective_settings", return_value=Settings(data_dir=Path(directory))
        ), patch("app.jobs.ImmichClient", return_value=client), patch(
            "app.jobs.db.get_job", return_value={"job_kind": "compress"}
        ), patch("app.jobs.db.update_job") as update_job, patch(
            "app.jobs.ios_problem_db.update_status"
        ) as update_status:
            process_asset("asset-2", threading.Event())

        update_job.assert_called_once_with(
            "asset-2",
            state="failed",
            error="Asset no longer exists in Immich: 404 Client Error",
        )
        update_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
