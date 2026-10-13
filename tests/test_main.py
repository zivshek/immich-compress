from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from app.jobs import IOS_REPAIR_JOB_KIND
from app.main import enqueue_ios_repairs
from app.photo_problems import photo_problem_view_asset_id


class PhotoProblemLinksTest(unittest.TestCase):
    def row(self, **values):
        return {"asset_id": "original", "target_asset_id": None,
                "manifest": None, "status": "problem", **values}

    def test_unrepaired_photo_links_to_original(self):
        self.assertEqual(photo_problem_view_asset_id(self.row()), "original")

    def test_failed_replacement_never_links_to_deleted_original(self):
        for stage in ("delete-requested", "deleted"):
            with self.subTest(stage=stage):
                row = self.row(status="failed", manifest=json.dumps({"stage": stage}))
                self.assertIsNone(photo_problem_view_asset_id(row))

    def test_recovers_uploaded_id_from_manifest_when_column_is_missing(self):
        row = self.row(status="failed", manifest=json.dumps({
            "stage": "uploaded", "target_id": "replacement",
        }))
        self.assertEqual(photo_problem_view_asset_id(row), "replacement")

    def test_repair_in_progress_has_no_transient_link(self):
        self.assertIsNone(photo_problem_view_asset_id(self.row(status="repairing")))

    def test_completed_repair_links_to_replacement(self):
        row = self.row(status="repaired", target_asset_id="replacement")
        self.assertEqual(photo_problem_view_asset_id(row), "replacement")


class MainHelpersTest(unittest.TestCase):
    def test_enqueue_ios_repairs_marks_missing_assets_failed_and_continues(self) -> None:
        client = MagicMock()
        client.find_asset_by_id.side_effect = [
            RuntimeError("gone"),
            {"id": "asset-2", "originalFileName": "b.MOV"},
        ]

        with patch("app.main.ImmichClient", return_value=client), patch(
            "app.main.job_queue.enqueue_asset"
        ) as enqueue, patch("app.main.ios_problem_db.update_status") as update_status:
            enqueue_ios_repairs(["asset-1", "asset-2"])

        enqueue.assert_called_once_with(
            {"id": "asset-2", "originalFileName": "b.MOV"},
            job_kind=IOS_REPAIR_JOB_KIND,
            force=True,
        )
        update_status.assert_called_once_with("asset-1", "failed", "gone")

    def test_enqueue_ios_repairs_does_not_touch_problem_rows_for_existing_assets(
        self,
    ) -> None:
        client = MagicMock()
        client.find_asset_by_id.side_effect = [{"id": "asset-1"}, {"id": "asset-2"}]

        with patch("app.main.ImmichClient", return_value=client), patch(
            "app.main.job_queue.enqueue_asset"
        ) as enqueue, patch("app.main.ios_problem_db.update_status") as update_status:
            enqueue_ios_repairs(["asset-1", "asset-2"])

        self.assertEqual(enqueue.call_count, 2)
        update_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
