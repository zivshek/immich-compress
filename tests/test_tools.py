from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import Settings
from app.tools import handbrake_encoders, handbrake_presets


class HandBrakeDiscoveryTest(unittest.TestCase):
    def test_discovers_presets_from_json(self) -> None:
        output = """
        [12:34:56] HandBrake 1.9.2
        {"PresetList":[
          {"Folder":true,"PresetName":"General","ChildrenArray":[
            {"Folder":false,"PresetName":"Fast 1080p30","PresetDescription":"HandBrake's fast preset description."},
            {"Folder":false,"PresetName":"HQ 1080p30 Surround"}
          ]}
        ]}
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_presets(Settings())

        self.assertEqual([option.value for option in options], ["Fast 1080p30", "HQ 1080p30 Surround"])
        self.assertEqual(options[0].description, "HandBrake's fast preset description.")

    def test_discovers_and_describes_encoders_from_help(self) -> None:
        output = """
          -e, --encoder <string>  Select video encoder
                  x264
                  x265
                  nvenc_h265
                  qsv_h265
          --encoder-preset <string>
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_encoders(Settings())

        self.assertEqual(
            [option.value for option in options],
            ["x264", "x265", "nvenc_h265", "qsv_h265"],
        )
        self.assertIn("NVIDIA", options[2].description)
        self.assertIn("Intel", options[3].description)

    def test_discovers_encoders_from_options_line(self) -> None:
        output = """
          -e, --encoder <string>  Select video encoder
              Options: x264, x265, nvenc_h265

          --encoder-preset <string>
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_encoders(Settings())

        self.assertEqual([option.value for option in options], ["x264", "x265", "nvenc_h265"])

    def test_discovers_presets_from_text_list(self) -> None:
        output = """
        General/
            Fast 1080p30
                Fast H.264 video compatible with common devices.
            HQ 1080p30 Surround
                High quality H.264 video with surround audio.
        Hardware/
            H.265 NVENC 1080p
                H.265 video using NVIDIA NVENC.
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_presets(Settings())

        self.assertEqual(
            [option.value for option in options],
            ["Fast 1080p30", "HQ 1080p30 Surround", "H.265 NVENC 1080p"],
        )
        self.assertEqual(
            options[0].description,
            "Fast H.264 video compatible with common devices.",
        )

    def test_does_not_treat_wrapped_descriptions_as_presets(self) -> None:
        output = """
        Devices/
            Apple 540p30 Surround
                Compatible with Apple iPhone 4, 4S, and later; iPod touch
                4th, 5th Generation and later; iPad 1st Generation and later.
            Chromecast 1080p60 Surround
                Compatible with Google Chromecast Ultra.
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_presets(Settings())

        self.assertEqual(
            [option.value for option in options],
            ["Apple 540p30 Surround", "Chromecast 1080p60 Surround"],
        )
        self.assertIn("iPod touch 4th, 5th Generation", options[0].description)


if __name__ == "__main__":
    unittest.main()
