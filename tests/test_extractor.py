"""Standard-library tests for the extraction layer."""

import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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

    def test_normalize_drops_long_sparse_whisper_hallucination(self):
        result = MediaExtractor._normalize_segments(
            [
                {
                    "id": 1,
                    "start": 59.68,
                    "end": 75.92,
                    "text": "难道是",
                    "avg_logprob": -0.9,
                    "no_speech_prob": 0.2,
                },
                {
                    "id": 2,
                    "start": 76.0,
                    "end": 79.0,
                    "text": "我们现在一起戴上头盔",
                    "avg_logprob": -0.2,
                    "no_speech_prob": 0.05,
                },
            ]
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "我们现在一起戴上头盔")
        self.assertEqual(result[0]["avg_logprob"], -0.2)

    def test_whisper_tail_is_clamped_to_real_media_duration(self):
        result = MediaExtractor._clamp_segments_to_duration(
            [
                {"id": 1, "start_sec": 10.0, "end_sec": 14.0, "text": "tail"},
                {"id": 2, "start_sec": 14.0, "end_sec": 16.0, "text": "hallucination"},
            ],
            12.5125,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["end_sec"], 12.512)

    def test_default_visual_policy_is_full_one_fps_coverage(self):
        self.assertEqual(self.extractor.sample_interval_sec, 1.0)
        self.assertEqual(self.extractor.min_keyframe_gap_sec, 1.0)
        self.assertEqual(self.extractor.max_keyframes, 7200)

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


    def test_silent_broll_is_detected_without_loading_whisper(self):
        completed = mock.Mock(returncode=0, stdout="")
        with (
            mock.patch("src.extractor.shutil.which", return_value="ffprobe"),
            mock.patch("src.extractor.subprocess.run", return_value=completed),
        ):
            self.assertFalse(
                MediaExtractor.has_audio_stream(Path("silent-broll.mp4"))
            )


if __name__ == "__main__":
    unittest.main()
