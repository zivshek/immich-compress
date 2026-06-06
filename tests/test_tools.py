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
            {"Folder":false,"PresetName":"Fast 1080p30"},
            {"Folder":false,"PresetName":"HQ 1080p30 Surround"}
          ]}
        ]}
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_presets(Settings())

        self.assertEqual([option.value for option in options], ["Fast 1080p30", "HQ 1080p30 Surround"])
        self.assertIn("Faster encoding", options[0].description)

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
            HQ 1080p30 Surround
        Hardware/
            H.265 NVENC 1080p
        """
        with patch("app.tools.run_handbrake", return_value=output):
            options = handbrake_presets(Settings())

        self.assertEqual(
            [option.value for option in options],
            ["Fast 1080p30", "HQ 1080p30 Surround", "H.265 NVENC 1080p"],
        )


if __name__ == "__main__":
    unittest.main()
