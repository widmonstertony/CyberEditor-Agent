"""Standard-library tests for the extraction layer."""

import logging
from pathlib import Path
import tempfile
import unittest

from src.extractor import ExtractionError, MediaExtractor


class MediaExtractorTests(unittest.TestCase):
    """Test helpers that do not require Whisper/OpenCV."""

    def setUp(self):
        self.extractor = MediaExtractor(
            whisper_model="tiny",
            logger=logging.getLogger("test.extractor"),
        )

    def test_srt_timestamp_rounding(self):
        self.assertEqual(
            MediaExtractor.format_srt_timestamp(0), "00:00:00,000"
        )
        self.assertEqual(
            MediaExtractor.format_srt_timestamp(3661.9996), "01:01:02,000"
        )

    def test_normalize_segments(self):
        result = MediaExtractor._normalize_segments(
            [
                {"id": 3, "start": 1.25, "end": 2.5, "text": "  hello   world "},
                {"id": 4, "start": 3, "end": 3, "text": "invalid"},
            ]
        )
        self.assertEqual(
            result,
            [
                {
                    "id": 3,
                    "start_sec": 1.25,
                    "end_sec": 2.5,
                    "text": "hello world",
                }
            ],
        )

    def test_normalize_rejects_empty_transcript(self):
        with self.assertRaises(ExtractionError):
            MediaExtractor._normalize_segments([])

    def test_write_srt_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "字幕.srt"
            MediaExtractor.write_srt(
                [
                    {
                        "start_sec": 1.0,
                        "end_sec": 2.25,
                        "text": "中文台词",
                    }
                ],
                destination,
            )
            value = destination.read_text(encoding="utf-8")
            self.assertIn("00:00:01,000 --> 00:00:02,250", value)
            self.assertIn("中文台词", value)


if __name__ == "__main__":
    unittest.main()
