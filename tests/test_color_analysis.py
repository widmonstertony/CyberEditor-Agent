"""Tests for deterministic project-level color matching."""

import unittest

from src.color_analysis import build_project_color_match, summarize_color_samples


class ColorAnalysisTests(unittest.TestCase):
    def test_sample_summary_prefers_confident_neutral_measurements(self):
        summary = summarize_color_samples(
            [
                {"median_luma": 0.20, "rgb_gain": [1.2, 1.0, 0.8], "confidence": 0.7},
                {"median_luma": 0.30, "rgb_gain": [1.0, 1.0, 1.0], "confidence": 0.1},
                {"median_luma": 0.22, "rgb_gain": [1.1, 1.0, 0.9], "confidence": 0.8},
            ]
        )
        self.assertEqual(summary["high_confidence_sample_count"], 2)
        self.assertAlmostEqual(summary["median_luma"], 0.21)
        self.assertAlmostEqual(summary["rgb_gain"][0], 1.15)

    def test_project_match_is_bounded_and_keyed_by_asset(self):
        match = build_project_color_match(
            [
                {
                    "asset_id": "dark",
                    "color_analysis": {
                        "median_luma": 0.1,
                        "rgb_gain": [1.4, 1.0, 0.7],
                        "confidence": 0.8,
                        "method": "neutral_median",
                    },
                },
                {
                    "asset_id": "bright",
                    "color_analysis": {
                        "median_luma": 0.4,
                        "rgb_gain": [0.9, 1.0, 1.1],
                        "confidence": 0.8,
                        "method": "neutral_median",
                    },
                },
            ]
        )
        self.assertTrue(match["enabled"])
        self.assertEqual(set(match["assets"]), {"dark", "bright"})
        self.assertLessEqual(match["assets"]["dark"]["exposure_ev"], 1.5)
        self.assertGreaterEqual(match["assets"]["bright"]["exposure_ev"], -1.5)


if __name__ == "__main__":
    unittest.main()
