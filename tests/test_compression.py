from __future__ import annotations

import unittest
from pathlib import Path

from app.compression import VideoInfo, build_av1_command, parse_command_percent
from app.config import Settings


class FixedCrfAv1Test(unittest.TestCase):
    def test_builds_4k_av1_command_with_fixed_crf(self) -> None:
        config = Settings(video_crf=31)

        command = build_av1_command(
            Path("/input/video.mov"),
            Path("/work/video-only.mp4"),
            VideoInfo(width=3840, height=2160, rotation=-90),
            config,
        )

        self.assertEqual(command[0], "/opt/av1/bin/ffmpeg")
        self.assertEqual(command[command.index("-c:v") + 1], "libsvtav1")
        self.assertEqual(command[command.index("-crf") + 1], "31")
        self.assertEqual(Path(command[-1]).name, "video-only.mp4")

    def test_builds_fixed_crf_command_for_non_4k_video(self) -> None:
        command = build_av1_command(
            Path("input.mp4"),
            Path("output.mp4"),
            VideoInfo(width=1920, height=1080, rotation=0),
            Settings(),
        )

        self.assertEqual(command[command.index("-crf") + 1], "28")

    def test_parses_progress_percentage(self) -> None:
        self.assertEqual(parse_command_percent("encoding 47%, eta 1 minute"), 47)
        self.assertEqual(parse_command_percent("108%, finishing"), 100)
        self.assertIsNone(parse_command_percent("frame=120 fps=8.2"))


if __name__ == "__main__":
    unittest.main()
