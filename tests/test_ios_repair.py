from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.ios_repair import (
    MediaProbe,
    analyze_ios_compatibility,
    build_ios_repair_command,
    build_tonemap_filter,
    copy_metadata_for_ios_repair,
)


class IosRepairTest(unittest.TestCase):
    def test_flags_vp9_profile_2_hlg_mov_as_problematic(self) -> None:
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
            handler_name="Google",
        )

        analysis = analyze_ios_compatibility(probe)

        self.assertTrue(analysis.needs_repair)
        self.assertTrue(any("VP9 Profile 2" in reason for reason in analysis.reasons))
        self.assertTrue(any("HDR" in reason for reason in analysis.reasons))

    def test_leaves_sdr_h264_mp4_with_aac_alone(self) -> None:
        probe = MediaProbe(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            video_codec="h264",
            video_profile="High",
            pixel_format="yuv420p",
            width=1920,
            height=1080,
            rotation=0,
            color_primaries="bt709",
            color_transfer="bt709",
            color_space="bt709",
            audio_codecs=("aac",),
            handler_name=None,
        )

        analysis = analyze_ios_compatibility(probe)

        self.assertFalse(analysis.needs_repair)
        self.assertEqual(analysis.reasons, ())

    def test_builds_nvenc_h264_aac_bt709_repair_command(self) -> None:
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
            handler_name="Google",
        )

        command = build_ios_repair_command(
            Path("input.mov"),
            Path("output.mp4"),
            probe,
            Settings(ffmpeg="ffmpeg", ios_repair_encoder="h264_nvenc", ios_repair_cq=21),
        )

        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(command[command.index("-cq") + 1], "21")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-color_trc") + 1], "bt709")
        self.assertIn("tonemap", command[command.index("-vf") + 1])

    def test_hdr_filter_tone_maps_to_bt709(self) -> None:
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
            handler_name="Google",
        )

        video_filter = build_tonemap_filter(probe)

        self.assertIn("tonemap=tonemap=hable", video_filter)
        self.assertIn("primaries=bt709", video_filter)
        self.assertIn("transfer=bt709", video_filter)

    def test_exiftool_copy_excludes_source_color_tags(self) -> None:
        copied_args = ""

        def capture_copy(command: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal copied_args
            args_file = Path(command[command.index("-@") + 1])
            copied_args = args_file.read_text(encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="")

        with patch("app.ios_repair.subprocess.run", side_effect=capture_copy):
            copy_metadata_for_ios_repair(Path("original.mov"), Path("repaired.mp4"), Settings())

        self.assertIn("--ColorPrimaries\n", copied_args)
        self.assertIn("--TransferCharacteristics\n", copied_args)


if __name__ == "__main__":
    unittest.main()
