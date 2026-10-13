from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

try:
    import requests
except ImportError:
    requests = ModuleType("requests")
    requests.Session = Mock
    requests.RequestException = Exception
    sys.modules.setdefault("requests", requests)

from app.config import settings
from app.immich import ImmichClient


class ImmichVideoSearchTest(unittest.TestCase):
    def test_photo_search_ignores_video_date_cutoff(self) -> None:
        client = ImmichClient(replace(settings, video_taken_before="2026-06-01"))
        client.request = Mock(return_value={"assets": {"items": []}})
        client.search_photos(page=3)
        query = client.request.call_args.kwargs["json"]
        self.assertEqual(query["type"], "IMAGE")
        self.assertEqual(query["page"], 3)
        self.assertNotIn("takenBefore", query)

    def test_header_probe_uses_range_and_closes_stream(self) -> None:
        client = ImmichClient(settings)
        client.session = MagicMock()
        response = client.session.get.return_value.__enter__.return_value
        response.iter_content.return_value = iter([b"\xff\xd8\xff" + b"x" * 61])
        self.assertEqual(len(client.original_header("photo-1")), 64)
        self.assertEqual(client.session.get.call_args.kwargs["headers"], {"Range": "bytes=0-63"})
        client.session.get.return_value.__exit__.assert_called_once()

    def test_jpeg_upload_sends_correct_mime_and_explicit_motion_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "IMG_4879.jpg"
            photo.write_bytes(b"\xff\xd8\xff")
            client = ImmichClient(settings)
            client.session = MagicMock()
            source = {"id": "photo-1", "fileCreatedAt": "date", "fileModifiedAt": "date"}
            client.upload_asset_copy(source, photo, live_photo_video_id="motion-1")
            kwargs = client.session.post.call_args.kwargs
            self.assertEqual(kwargs["files"]["assetData"][2], "image/jpeg")
            self.assertEqual(kwargs["data"]["livePhotoVideoId"], "motion-1")

    def test_filename_search_bypasses_default_taken_before_cutoff(self) -> None:
        client = ImmichClient(replace(settings, video_taken_before="2026-06-01T12:00:00.000Z"))
        client.request = Mock(return_value={"assets": {"items": [], "total": 0}})

        client.search_videos(page=2, size=10, file_name="PXL_202606")

        query = client.request.call_args.kwargs["json"]
        self.assertEqual(query["originalFileName"], "PXL_202606")
        self.assertNotIn("takenBefore", query)

    def test_default_video_search_uses_taken_before_cutoff(self) -> None:
        cutoff = "2026-06-01T12:00:00.000Z"
        client = ImmichClient(replace(settings, video_taken_before=cutoff))
        client.request = Mock(return_value={"assets": {"items": [], "total": 0}})

        client.search_videos()

        query = client.request.call_args.kwargs["json"]
        self.assertEqual(query["takenBefore"], cutoff)
        self.assertNotIn("originalFileName", query)


if __name__ == "__main__":
    unittest.main()
