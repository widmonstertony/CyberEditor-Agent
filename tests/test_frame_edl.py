"""Regression tests for the one canonical source/record-frame schedule."""

from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import tempfile
import unittest

from src.frame_edl import (
    FrameEDLError,
    build_frame_edl,
    map_original_time_to_record_frame,
    validate_frame_edl,
)


class FrameEDLTests(unittest.TestCase):
    """Protect mixed-FPS and long-cut-list timing from cumulative drift."""

    def test_mixed_fps_schedule_is_contiguous_and_preserves_all_source_frames(self):
        ntsc = Decimal(60000) / Decimal(1001)
        payload = {
            "project_fps": "59.94005994",
            "clips": [
                {
                    "clip_id": "A",
                    "file_name": "a.mp4",
                    "cut_in_sec": 0.02,
                    "cut_out_sec": 1.03,
                },
                {
                    "clip_id": "B",
                    "file_name": "b.mp4",
                    "cut_in_sec": 2.013,
                    "cut_out_sec": 3.777,
                },
            ],
        }

        schedule = build_frame_edl(
            payload, source_fps_overrides=[Decimal(25), ntsc]
        )
        payload["frame_edl"] = schedule

        self.assertEqual(schedule["clips"][0]["source_frame_in"], 0)
        self.assertEqual(schedule["clips"][0]["source_frame_out_exclusive"], 26)
        self.assertEqual(schedule["clips"][0]["record_frame_count"], 63)
        self.assertEqual(
            schedule["clips"][0]["record_frame_out_exclusive"],
            schedule["clips"][1]["record_frame_in"],
        )
        self.assertEqual(
            validate_frame_edl(payload)["total_record_frames"],
            sum(item["record_frame_count"] for item in schedule["clips"]),
        )

    def test_hundreds_of_fractional_cuts_have_one_exact_record_total(self):
        ntsc = Decimal(60000) / Decimal(1001)
        clips = []
        rates = []
        for index in range(240):
            rate = Decimal(25) if index % 2 == 0 else ntsc
            start = Decimal(index % 7) / Decimal(100) + Decimal("0.011")
            duration = Decimal("0.337") + Decimal(index % 5) / Decimal(1000)
            clips.append(
                {
                    "clip_id": index + 1,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": str(start),
                    "cut_out_sec": str(start + duration),
                }
            )
            rates.append(rate)
        payload = {"project_fps": 25, "clips": clips}

        schedule = build_frame_edl(payload, source_fps_overrides=rates)
        expected = 0
        for clip, rate in zip(clips, rates):
            start = Decimal(clip["cut_in_sec"])
            end = Decimal(clip["cut_out_sec"])
            source_in = int(start * rate)
            source_out = int((end * rate).to_integral_value(rounding=ROUND_CEILING))
            expected += int(
                (Decimal(source_out - source_in) * Decimal(25) / rate).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )

        self.assertEqual(schedule["total_record_frames"], expected)
        self.assertEqual(schedule["clips"][-1]["record_frame_out_exclusive"], expected)
        self.assertNotAlmostEqual(
            float(Decimal(expected) / Decimal(25)),
            sum(float(Decimal(c["cut_out_sec"]) - Decimal(c["cut_in_sec"])) for c in clips),
            places=3,
        )

    def test_stale_schedule_is_rejected_after_edit_change(self):
        payload = {
            "project_fps": 25,
            "clips": [{
                "clip_id": 1,
                "file_name": "source.mp4",
                "cut_in_sec": 0,
                "cut_out_sec": 2,
            }],
        }
        payload["frame_edl"] = build_frame_edl(
            payload, source_fps_overrides=[25]
        )
        payload["clips"][0]["cut_out_sec"] = 3

        with self.assertRaisesRegex(FrameEDLError, "fingerprint"):
            validate_frame_edl(payload)

    def test_original_boundary_maps_to_exact_record_boundary(self):
        payload = {
            "project_fps": 25,
            "clips": [
                {
                    "clip_id": 1, "file_name": "a.mp4",
                    "cut_in_sec": 0.01, "cut_out_sec": 1.01,
                },
                {
                    "clip_id": 2, "file_name": "b.mp4",
                    "cut_in_sec": 0.02, "cut_out_sec": 1.02,
                },
            ],
        }
        schedule = build_frame_edl(payload, source_fps_overrides=[25, 25])

        self.assertEqual(
            map_original_time_to_record_frame(schedule, 1.0, rounding="nearest"),
            schedule["clips"][0]["record_frame_out_exclusive"],
        )
        self.assertEqual(
            map_original_time_to_record_frame(schedule, 2.0, rounding="ceil"),
            schedule["total_record_frames"],
        )


if __name__ == "__main__":
    unittest.main()
