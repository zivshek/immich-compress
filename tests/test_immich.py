from __future__ import annotations

import unittest
from dataclasses import replace
from types import ModuleType
from unittest.mock import Mock
import sys

requests = ModuleType("requests")
requests.Session = Mock
requests.RequestException = Exception
sys.modules.setdefault("requests", requests)

from app.config import settings
from app.immich import ImmichClient


class ImmichVideoSearchTest(unittest.TestCase):
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
