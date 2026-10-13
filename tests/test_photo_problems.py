from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

from app import photo_problem_db as store
from app.config import settings
from app.photo_problems import (
    PhotoProblemWorker,
    repair_photo,
    safe_name,
    upload_photo,
    verify_jpeg,
)


def checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


class FakeImmich:
    def __init__(self, directory: Path, *, live: bool = False):
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (40, 30)).save(buffer, format="JPEG", exif=exif)
        self.photo_bytes = buffer.getvalue()
        self.video_bytes = b"motion-original-bytes"
        self.config = replace(settings, data_dir=directory, immich_url="http://test")
        self.asset = {
            "id": "photo-1",
            "originalFileName": "IMG_4879.HEIC",
            "type": "IMAGE",
            "checksum": checksum(self.photo_bytes),
            "fileCreatedAt": "2022-08-04T19:40:21Z",
            "fileModifiedAt": "2022-08-04T19:40:21Z",
            "isFavorite": True,
            "exifInfo": {"description": "Caption", "latitude": 45, "longitude": -75},
            "livePhotoVideoId": "video-1" if live else None,
            "tags": [{"id": "tag-1"}],
        }
        self.video = {
            **self.asset,
            "id": "video-1",
            "originalFileName": "IMG_4879.MOV",
            "type": "VIDEO",
            "checksum": checksum(self.video_bytes),
            "livePhotoVideoId": None,
        }
        self.assets = {"photo-1": self.asset.copy()}
        if live:
            self.assets["video-1"] = self.video.copy()
        self.events = []
        self.fail_upload = False
        self.fail_restore = False
        self.bad_video_download = False
        self.lost_delete_response = False

    def find_asset_by_id(self, asset_id):
        if asset_id not in self.assets:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError(response=response)
        return self.assets[asset_id].copy()

    def download_original(self, asset_id, path):
        self.events.append(("download", asset_id))
        data = self.photo_bytes if asset_id == "photo-1" else self.video_bytes
        if asset_id == "video-1" and self.bad_video_download:
            data = b"partial"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def album_memberships(self, ids):
        return {asset_id: [{"id": "album-1"}] for asset_id in ids}

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "shared-links":
            return [{"id": "link-1", "assets": [{"id": "photo-1"}]}]
        self.events.append((method, path, kwargs))
        return []

    def update_asset(self, asset_id, values):
        self.events.append(("update", asset_id, values))
        if asset_id == "photo-new" and self.fail_restore:
            raise RuntimeError("metadata restore failed")
        self.assets[asset_id].update(values)

    def delete_asset_permanently(self, asset_id):
        row = store.get(asset_id)
        assert row["manifest"], "Deletion must have durable recovery state"
        self.events.append(("delete", asset_id))
        motion = self.assets[asset_id].get("livePhotoVideoId")
        if motion:
            del self.assets[motion]  # Model Immich's Live Photo cascading deletion.
        del self.assets[asset_id]
        if self.lost_delete_response:
            raise RuntimeError("connection lost after deletion")

    def upload_asset_copy(self, asset, path, *, live_photo_video_id=None):
        self.events.append(("upload", asset["id"]))
        if asset["type"] == "VIDEO":
            self.assets["video-1"] = self.video.copy()
            return {"id": "video-1", "status": "duplicate"}
        if self.fail_upload:
            raise RuntimeError("upload interrupted")
        assert path.read_bytes() == self.photo_bytes
        self.assets["photo-new"] = {
            **asset,
            "id": "photo-new",
            "originalFileName": path.name,
            "livePhotoVideoId": live_photo_video_id,
        }
        return {"id": "photo-new", "status": "created"}

    def add_asset_to_album(self, asset_id, album_id):
        self.events.append(("album", asset_id, album_id))


class PhotoRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.patch = patch.object(store, "settings", replace(settings, data_dir=self.directory))
        self.patch.start()
        store.init_db()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def repair(self, client):
        store.record(client.asset, "problem")
        repair_photo(client, "photo-1")

    def test_still_repair_preserves_bytes_orientation_and_metadata(self):
        client = FakeImmich(self.directory)
        self.repair(client)
        row = store.get("photo-1")
        self.assertEqual(row["status"], "repaired")
        self.assertEqual(row["target_asset_id"], "photo-new")
        manifest = json.loads(row["manifest"])
        path = Path(manifest["files"]["photo"]["path"])
        self.assertEqual(path.read_bytes(), client.photo_bytes)
        with Image.open(path) as image:
            self.assertEqual(image.getexif()[274], 6)
        self.assertTrue(path.with_suffix(".HEIC").is_file())
        self.assertEqual(client.assets["photo-new"]["description"], "Caption")
        self.assertIn(("album", "photo-new", "album-1"), client.events)
        self.assertTrue(
            any(event[:2] == ("PUT", "shared-links/link-1/assets") for event in client.events)
        )
        self.assertTrue(any(event[:2] == ("PUT", "tags/tag-1/assets") for event in client.events))

    def test_live_photo_downloads_both_before_deletion_and_detaches_motion(self):
        client = FakeImmich(self.directory, live=True)
        self.repair(client)
        deletion = client.events.index(("delete", "photo-1"))
        self.assertLess(client.events.index(("download", "video-1")), deletion)
        self.assertLess(
            client.events.index(("update", "photo-1", {"livePhotoVideoId": None})), deletion
        )
        self.assertIn("video-1", client.assets)
        self.assertEqual(client.assets["photo-new"]["livePhotoVideoId"], "video-1")
        manifest = json.loads(store.get("photo-1")["manifest"])
        self.assertEqual(Path(manifest["files"]["video"]["path"]).read_bytes(), client.video_bytes)

    def test_incomplete_motion_download_never_deletes_original(self):
        client = FakeImmich(self.directory, live=True)
        client.bad_video_download = True
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.repair(client)
        self.assertIn("photo-1", client.assets)
        self.assertFalse(any(event[0] == "delete" for event in client.events))

    def test_retry_after_original_deleted_uses_saved_backup(self):
        client = FakeImmich(self.directory, live=True)
        client.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
            self.repair(client)
        self.assertNotIn("photo-1", client.assets)
        client.fail_upload = False
        downloads = sum(event[0] == "download" for event in client.events)
        repair_photo(client, "photo-1")
        self.assertEqual(store.get("photo-1")["status"], "repaired")
        self.assertEqual(sum(event[0] == "download" for event in client.events), downloads)

    def test_lost_delete_response_can_resume_without_original(self):
        client = FakeImmich(self.directory, live=True)
        client.lost_delete_response = True
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            self.repair(client)
        repair_photo(client, "photo-1")
        self.assertEqual(store.get("photo-1")["status"], "repaired")

    def test_metadata_failure_retry_does_not_upload_again(self):
        client = FakeImmich(self.directory)
        client.fail_restore = True
        with self.assertRaisesRegex(RuntimeError, "metadata restore"):
            self.repair(client)
        client.fail_restore = False
        repair_photo(client, "photo-1")
        self.assertEqual(client.events.count(("upload", "photo-1")), 1)
        self.assertEqual(store.get("photo-1")["status"], "repaired")

    def test_modified_backup_stops_retry(self):
        client = FakeImmich(self.directory)
        client.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.repair(client)
        manifest = json.loads(store.get("photo-1")["manifest"])
        Path(manifest["files"]["photo"]["path"]).write_bytes(b"corrupt")
        client.fail_upload = False
        with self.assertRaisesRegex(RuntimeError, "Backup|backup"):
            repair_photo(client, "photo-1")
        self.assertNotIn("photo-new", client.assets)

    def test_clear_and_restart_keep_recovery_state(self):
        client = FakeImmich(self.directory)
        client.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.repair(client)
        store.update("photo-1", status="repairing")
        store.record({"id": "clean-1"}, "clean")
        store.init_db()
        self.assertEqual(store.get("photo-1")["status"], "failed")
        store.clear_scan_cache()
        self.assertIsNotNone(store.get("photo-1")["manifest"])
        self.assertIsNone(store.get("clean-1"))

    def test_scanner_checks_only_heic_and_caches_real_heic(self):
        client = FakeImmich(self.directory)
        client.search_photos = lambda page: [
            client.asset,
            {"id": "heic-1", "originalFileName": "real.heic"},
            {"id": "jpg-1", "originalFileName": "normal.jpg"},
        ]
        calls = []

        def header(asset_id):
            calls.append(asset_id)
            return client.photo_bytes[:64] if asset_id == "photo-1" else b"\x00\x00\x00\x18ftypheic"

        client.original_header = header
        worker = PhotoProblemWorker()
        worker._scan(client)
        worker._scan(client)
        self.assertEqual(calls, ["photo-1", "heic-1"])
        self.assertEqual(store.get("photo-1")["status"], "problem")
        self.assertEqual(store.get("heic-1")["status"], "clean")
        self.assertEqual(store.count(), 1)

    def test_filename_and_jpeg_validation(self):
        self.assertEqual(safe_name("../../IMG_4879.HEIC"), "IMG_4879.HEIC")
        self.assertEqual(safe_name("C:\\folder\\IMG_4879.HEIC"), "IMG_4879.HEIC")
        path = self.directory / "bad.jpg"
        path.write_bytes(b"\xff\xd8\xffbroken")
        with self.assertRaises(OSError):
            verify_jpeg(path)

    def test_duplicate_original_is_retried_not_reported_as_repaired(self):
        client = FakeImmich(self.directory)
        client.assets["photo-new"] = {
            **client.asset,
            "id": "photo-new",
            "originalFileName": "IMG_4879.jpg",
        }
        manifest = {"asset": client.asset, "files": {"photo": {"path": "unused.jpg"}}}
        replies = iter([{"id": "photo-1"}, {"id": "photo-new"}])
        client.upload_asset_copy = lambda *args, **kwargs: next(replies)
        with patch("app.photo_problems.time.sleep") as sleep:
            self.assertEqual(upload_photo(client, manifest, None), "photo-new")
        sleep.assert_called_once_with(2)

    def test_real_heic_is_never_deleted(self):
        client = FakeImmich(self.directory)
        client.photo_bytes = b"\x00\x00\x00\x18ftypheic" + b"not-a-jpeg"
        client.asset["checksum"] = checksum(client.photo_bytes)
        client.assets["photo-1"] = client.asset.copy()
        with self.assertRaises((OSError, RuntimeError)):
            self.repair(client)
        self.assertIn("photo-1", client.assets)
        self.assertFalse(any(event[0] == "delete" for event in client.events))

    def test_external_and_stacked_photos_stop_before_download(self):
        for field in ("isExternal", "stackId"):
            with self.subTest(field=field):
                client = FakeImmich(self.directory)
                client.assets["photo-1"][field] = True
                with self.assertRaises(RuntimeError):
                    self.repair(client)
                self.assertEqual(client.events, [])

    def test_pairing_failure_is_not_reported_as_success(self):
        client = FakeImmich(self.directory, live=True)
        original_update = client.update_asset

        def broken_pairing(asset_id, values):
            original_update(asset_id, values)
            if asset_id == "photo-new":
                client.assets[asset_id]["livePhotoVideoId"] = None

        client.update_asset = broken_pairing
        with self.assertRaisesRegex(RuntimeError, "pairing verification"):
            self.repair(client)
        self.assertNotEqual(store.get("photo-1")["status"], "repaired")
        self.assertIsNotNone(store.get("photo-1")["manifest"])

    def test_page_renders_problem_and_backup_actions(self):
        from starlette.requests import Request

        from app.main import photo_problems_page

        client = FakeImmich(self.directory, live=True)
        self.repair(client)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/photo-problems",
                "headers": [],
                "query_string": b"",
            }
        )
        with (
            patch("app.main.processing_is_configured", return_value=True),
            patch(
                "app.main.effective_settings",
                return_value=client.config,
            ),
        ):
            response = photo_problems_page(request, page=1, status="")
        html = response.body.decode()
        self.assertIn("Photo Problems", html)
        self.assertIn("Download motion backup", html)
        self.assertIn("Recovery metadata", html)
        self.assertIn("http://test/photos/photo-new", html)

    def test_http_selected_repairs_and_backup_download(self):
        from fastapi.testclient import TestClient

        from app.main import app

        immich = FakeImmich(self.directory, live=True)
        self.repair(immich)
        client = TestClient(app)
        try:
            with (
                patch("app.main.processing_is_configured", return_value=True),
                patch("app.main.effective_settings", return_value=immich.config),
                patch("app.main.photo_problem_worker.start") as start,
            ):
                response = client.post(
                    "/photo-problems/repair-selected",
                    data={"asset_ids": ["photo-1", "photo-2"]},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                start.assert_called_once_with(["photo-1", "photo-2"])
                response = client.get("/photo-problems/photo-1/backup/video")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, immich.video_bytes)
                response = client.get("/photo-problems/photo-1/backup/manifest")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["stage"], "complete")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
