"""Standard-library tests for the human-reference editorial benchmark."""

import json
from pathlib import Path
import tempfile
import unittest

from src.editorial_eval import EditorialEvalError, EditorialEvaluator, main


class EditorialEvaluatorTests(unittest.TestCase):
    """Verify metric definitions and actionable validation errors."""

    def setUp(self):
        self.evaluator = EditorialEvaluator()

    @staticmethod
    def timeline(clips, target=None):
        payload = {"schema_version": "1.0", "clips": clips}
        if target is not None:
            payload["target_duration_sec"] = target
        return payload

    @staticmethod
    def clip(clip_id, source_id, cut_in, cut_out):
        return {
            "clip_id": clip_id,
            "source_id": source_id,
            "cut_in_sec": cut_in,
            "cut_out_sec": cut_out,
        }

    def parse(self, payload, label="test", allow_empty=False):
        return self.evaluator.parse_timeline(payload, label=label, allow_empty=allow_empty)

    def test_perfect_edit_scores_one_and_zero_error(self):
        clips = [
            self.clip("one", "A", 0.0, 4.0),
            self.clip("two", "B", 10.0, 13.0),
            self.clip("three", "A", 20.0, 22.0),
        ]
        report = self.evaluator.evaluate(
            self.parse(self.timeline(clips), label="ai"),
            self.parse(self.timeline(clips), label="reference"),
        )

        metrics = report["metrics"]
        self.assertEqual(metrics["source_selection"]["precision"], 1.0)
        self.assertEqual(metrics["source_selection"]["recall"], 1.0)
        self.assertEqual(metrics["source_selection"]["f1"], 1.0)
        self.assertEqual(metrics["boundary_accuracy"]["boundary_mae_sec"], 0.0)
        self.assertEqual(metrics["order_consistency"]["score"], 1.0)
        self.assertEqual(metrics["repetition"]["duplicate_clip_ratio"], 0.0)
        self.assertEqual(metrics["duration"]["absolute_error_sec"], 0.0)

    def test_reports_selection_boundaries_order_repetition_and_duration(self):
        reference = self.timeline(
            [
                self.clip("r-a", "A", 0.0, 4.0),
                self.clip("r-b", "B", 0.0, 4.0),
                self.clip("r-c", "C", 0.0, 2.0),
            ],
            target=12.0,
        )
        ai = self.timeline(
            [
                self.clip("a-b", "B", 0.5, 4.5),
                self.clip("a-a", "A", 0.0, 3.0),
                self.clip("a-a-repeat", "A", 0.1, 3.1),
                self.clip("a-d", "D", 5.0, 7.0),
            ]
        )

        report = self.evaluator.evaluate(
            self.parse(ai, label="ai"),
            self.parse(reference, label="reference"),
        )
        metrics = report["metrics"]
        self.assertAlmostEqual(metrics["source_selection"]["precision"], 2 / 3, places=6)
        self.assertAlmostEqual(metrics["source_selection"]["recall"], 2 / 3, places=6)
        self.assertAlmostEqual(metrics["source_selection"]["f1"], 2 / 3, places=6)
        self.assertEqual(metrics["boundary_accuracy"]["matched_clip_count"], 2)
        self.assertEqual(metrics["boundary_accuracy"]["boundary_mae_sec"], 0.5)
        self.assertEqual(metrics["order_consistency"]["score"], 0.0)
        self.assertEqual(metrics["repetition"]["duplicate_clip_count"], 1)
        self.assertAlmostEqual(metrics["repetition"]["adjacent_same_source_ratio"], 1 / 3, places=6)
        self.assertEqual(metrics["duration"]["ai_duration_sec"], 12.0)
        self.assertEqual(metrics["duration"]["absolute_error_sec"], 0.0)
        self.assertEqual(report["unmatched_ai_clip_ids"], ["a-a-repeat", "a-d"])
        self.assertEqual(report["unmatched_reference_clip_ids"], ["r-c"])

    def test_filename_basename_is_cross_machine_and_case_insensitive(self):
        ai = self.timeline(
            [{"file_name": r"C:\proxy\SCENE_01.MP4", "cut_in_sec": 1, "cut_out_sec": 2}]
        )
        reference = self.timeline(
            [{"file_name": "/mnt/reference/scene_01.mp4", "cut_in_sec": 1, "cut_out_sec": 2}]
        )
        report = self.evaluator.evaluate(
            self.parse(ai, label="ai"),
            self.parse(reference, label="reference"),
        )
        self.assertEqual(report["metrics"]["source_selection"]["f1"], 1.0)
        self.assertEqual(report["metrics"]["boundary_accuracy"]["matched_clip_count"], 1)

    def test_empty_ai_timeline_is_scored_instead_of_crashing(self):
        ai = self.parse(self.timeline([]), label="ai", allow_empty=True)
        reference = self.parse(
            self.timeline([self.clip("r-a", "A", 0.0, 2.0)]),
            label="reference",
        )
        report = self.evaluator.evaluate(ai, reference)
        self.assertEqual(report["metrics"]["source_selection"]["f1"], 0.0)
        self.assertIsNone(report["metrics"]["boundary_accuracy"]["boundary_mae_sec"])
        self.assertEqual(report["metrics"]["duration"]["absolute_error_percent"], 100.0)

    def test_missing_fields_raise_bilingual_actionable_error(self):
        with self.assertRaisesRegex(EditorialEvalError, "cut_out_sec.*出点"):
            self.parse(
                self.timeline(
                    [
                        {
                            "source_id": "A",
                            "cut_in_sec": 0.0,
                        }
                    ]
                ),
                label="human reference",
            )
        with self.assertRaisesRegex(EditorialEvalError, "source identity.*素材身份"):
            self.parse(
                self.timeline([{"cut_in_sec": 0.0, "cut_out_sec": 1.0}]),
                label="human reference",
            )

    def test_cli_writes_report_and_returns_nonzero_for_bad_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ai_path = root / "timeline_cuts.json"
            reference_path = root / "human_reference.json"
            output_path = root / "report.json"
            payload = self.timeline([self.clip("one", "A", 1.0, 2.0)])
            ai_path.write_text(json.dumps(payload), encoding="utf-8")
            reference_path.write_text(json.dumps(payload), encoding="utf-8")

            exit_code = main(
                [
                    "--ai",
                    str(ai_path),
                    "--reference",
                    str(reference_path),
                    "--output",
                    str(output_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8"))["metrics"]["source_selection"]["f1"],
                1.0,
            )

            bad_exit = main(
                [
                    "--ai",
                    str(root / "missing.json"),
                    "--reference",
                    str(reference_path),
                ]
            )
            self.assertEqual(bad_exit, 2)


if __name__ == "__main__":
    unittest.main()
