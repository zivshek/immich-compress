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

    def test_leaves_h264_with_unfriendly_audio_alone(self) -> None:
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
            audio_codecs=("opus",),
            handler_name=None,
        )

        analysis = analyze_ios_compatibility(probe)

        self.assertFalse(analysis.needs_repair)
        self.assertEqual(analysis.reasons, ())

    def test_leaves_non_profile_2_vp9_sdr_alone(self) -> None:
        probe = MediaProbe(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            video_codec="vp9",
            video_profile="Profile 0",
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

    def test_leaves_hevc_hdr_alone(self) -> None:
        probe = MediaProbe(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            video_codec="hevc",
            video_profile="Main 10",
            pixel_format="yuv420p10le",
            width=3840,
            height=2160,
            rotation=0,
            color_primaries="bt2020",
            color_transfer="smpte2084",
            color_space="bt2020nc",
            audio_codecs=("aac",),
            handler_name=None,
        )

        analysis = analyze_ios_compatibility(probe)

        self.assertFalse(analysis.needs_repair)
        self.assertEqual(analysis.reasons, ())

    def test_builds_av1_nvenc_command_with_compression_crf(self) -> None:
        command = build_ios_repair_command(
            Path("input.mov"),
            Path("output.mp4"),
            Settings(ffmpeg="ffmpeg", ios_repair_encoder="av1_nvenc", video_crf=28),
        )

        self.assertEqual(command[command.index("-c:v") + 1], "av1_nvenc")
        self.assertEqual(command[command.index("-cq") + 1], "28")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "p010le")
        self.assertEqual(command[command.index("-tag:v") + 1], "av01")
        self.assertNotIn("-profile:v", command)
        self.assertNotIn("-vf", command)
        self.assertNotIn("tonemap", command)

    def test_builds_hevc_nvenc_command_with_hvc1_tag(self) -> None:
        command = build_ios_repair_command(
            Path("input.mov"),
            Path("output.mp4"),
            Settings(ffmpeg="ffmpeg", ios_repair_encoder="hevc_nvenc"),
        )

        self.assertEqual(command[command.index("-c:v") + 1], "hevc_nvenc")
        self.assertEqual(command[command.index("-profile:v") + 1], "main10")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "p010le")
        self.assertEqual(command[command.index("-tag:v") + 1], "hvc1")

    def test_builds_h264_nvenc_command_without_stream_tag(self) -> None:
        command = build_ios_repair_command(
            Path("input.mov"),
            Path("output.mp4"),
            Settings(ffmpeg="ffmpeg", ios_repair_encoder="h264_nvenc"),
        )

        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(command[command.index("-profile:v") + 1], "high")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertNotIn("-tag:v", command)

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
