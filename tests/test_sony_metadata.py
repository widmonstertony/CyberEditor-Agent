"""Tests for Sony sidecar parsing and color-profile mapping."""

from pathlib import Path
import tempfile
import unittest

from src.sony_metadata import (
    detect_sony_color_metadata,
    map_resolve_input_transform,
)


class SonyMetadataTests(unittest.TestCase):
    def test_slog_profiles_map_to_distinct_resolve_transforms(self):
        slog2 = map_resolve_input_transform("s-log2", "s-gamut")
        slog3 = map_resolve_input_transform("s-log3-cine", "s-gamut3-cine")

        self.assertEqual(slog2["resolve_input_gamma"], "S-Log2")
        self.assertEqual(slog3["resolve_input_gamma"], "S-Log3")
        self.assertNotEqual(slog2["camera_profile"], slog3["camera_profile"])

    def test_companion_m01_xml_is_detected_and_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "C1904.MP4"
            video.write_bytes(b"video")
            (root / "C1904M01.XML").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:test">
  <VideoFormat><VideoFrame videoCodec="HEVC" captureFps="59.94p" formatFps="59.94p"/></VideoFormat>
  <Device manufacturer="Sony" modelName="ILCE-7M4"/>
  <AcquisitionRecord><Group name="CameraUnitMetadataSet">
    <Item name="CaptureGammaEquation" value="s-log2"/>
    <Item name="CaptureColorPrimaries" value="s-gamut"/>
    <Item name="CodingEquations" value="rec709"/>
  </Group></AcquisitionRecord>
</NonRealTimeMeta>""",
                encoding="utf-8",
            )

            result = detect_sony_color_metadata(video)

            self.assertEqual(result["camera_model"], "ILCE-7M4")
            self.assertEqual(result["camera_profile"], "sony_slog2_sgamut")
            self.assertEqual(result["resolve_input_gamma"], "S-Log2")
            self.assertEqual(result["confidence"], 1.0)

    def test_missing_sidecar_is_explicitly_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"video")
            result = detect_sony_color_metadata(video)
            self.assertEqual(result["camera_profile"], "unknown")
            self.assertFalse(result["transform_supported"])


if __name__ == "__main__":
    unittest.main()
