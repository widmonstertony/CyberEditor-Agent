"""Standard-library tests for rendered rough-cut blind review."""

import json
import logging
from pathlib import Path
import tempfile
import types
import unittest

from src.rough_cut_reviewer import (
    PREVIEW_REVIEW_SCHEMA,
    RoughCutReviewer,
    VISUAL_BATCH_SCHEMA,
)


class FakeDirector:
    def __init__(self):
        self.prompts = []
        self.unloaded = []

    def check_ollama(self, require_vision=False):
        self.require_vision = require_vision

    def _request_json(
        self, prompt, schema, images=(), model=None, progress_activity="director_generation"
    ):
        self.prompts.append((prompt, schema, list(images), progress_activity))
        if schema is VISUAL_BATCH_SCHEMA:
            return {
                "literal_visual_summary": "A rider moves from preparation to a held pose.",
                "observed_actions": ["The rider adjusts equipment", "The rider holds a pose"],
                "visible_state_changes": ["Equipment changes from loose to ready"],
                "continuity_observations": [],
                "visible_text": [],
            }
        if schema is PREVIEW_REVIEW_SCHEMA:
            return {
                "inferred_form": "character_vignette",
                "literal_synopsis": "A rider prepares and finishes in a deliberate portrait.",
                "subject": "The rider",
                "apparent_goal": "Become ready for the portrait",
                "progression": ["Preparation", "Adjustment", "Final pose"],
                "ending": "The rider holds the completed portrait pose.",
                "takeaway_guess": "Preparation creates the final image.",
                "coherence_score": 8,
                "causal_clarity_score": 7,
                "visual_payoff_score": 8,
                "pacing_score": 8,
                "audio_story_score": 7,
                "confusing_transitions": [],
                "unsupported_or_unresolved_points": [],
                "required_changes": [],
                "passes": True,
                "reason": "The visible progression and ending are clear.",
            }
        raise AssertionError(schema)

    def unload_model(self, model=None):
        self.unloaded.append(model)


class RoughCutReviewerTests(unittest.TestCase):
    def timeline(self):
        return {
            "clips": [
                {
                    "file_name": "source.mp4",
                    "cut_in_sec": 10.0,
                    "cut_out_sec": 14.0,
                    "audio_intent": "preserve_dialogue",
                    "dialogue_ranges_sec": [
                        {"start_sec": 11.0, "end_sec": 12.5, "text": "We are ready"}
                    ],
                },
                {
                    "file_name": "source.mp4",
                    "cut_in_sec": 20.0,
                    "cut_out_sec": 23.0,
                    "audio_intent": "mute_for_music",
                    "dialogue_ranges_sec": [
                        {"start_sec": 20.0, "end_sec": 23.0, "text": "production talk"}
                    ],
                },
            ],
            "music_plan": {"cues": [], "silence_regions": []},
            "director_treatment": {"title": "must never enter blind prompt"},
        }

    def test_audible_program_uses_program_time_and_respects_mute(self):
        result = RoughCutReviewer._audible_program(self.timeline())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timeline_in_sec"], 1.0)
        self.assertEqual(result[0]["timeline_out_sec"], 2.5)
        self.assertEqual(result[0]["text"], "We are ready")

    def test_rendered_review_is_blind_and_publishes_deterministic_pass(self):
        reviewer = RoughCutReviewer.__new__(RoughCutReviewer)
        reviewer.model = "vision-model"
        reviewer.text_model = "text-model"
        reviewer.logger = logging.getLogger("test.rough")
        reviewer.director = FakeDirector()

        def fake_extract(this, preview, destination, duration):
            this._sample_fps = 1.0
            frames = []
            for index in range(3):
                frame = destination / f"frame_{index:05d}.jpg"
                frame.write_bytes(b"jpeg")
                frames.append(frame)
            return frames

        reviewer._extract_frames = types.MethodType(fake_extract, reviewer)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preview = root / "preview.mp4"
            preview.write_bytes(b"video")
            timeline = root / "timeline.json"
            timeline.write_text(json.dumps(self.timeline()), encoding="utf-8")
            output = root / "review.json"

            result = reviewer.review(preview, timeline, output)

        self.assertTrue(result["passes"])
        self.assertEqual(result["frame_count"], 3)
        self.assertTrue(reviewer.director.require_vision)
        final_prompt = reviewer.director.prompts[-1][0]
        self.assertNotIn("must never enter blind prompt", final_prompt)
        self.assertIn("LITERAL AUDIBLE DIALOGUE", final_prompt)
        self.assertIn("vision-model", reviewer.director.unloaded)
        self.assertIn("text-model", reviewer.director.unloaded)

    def test_failed_upstream_quality_gate_cannot_be_overruled_by_model(self):
        reviewer = RoughCutReviewer.__new__(RoughCutReviewer)
        reviewer.model = "vision-model"
        reviewer.text_model = "text-model"
        reviewer.logger = logging.getLogger("test.rough.gate")
        reviewer.director = FakeDirector()

        def fake_extract(this, preview, destination, duration):
            this._sample_fps = 1.0
            frames = []
            for index in range(3):
                frame = destination / f"frame_{index:05d}.jpg"
                frame.write_bytes(b"jpeg")
                frames.append(frame)
            return frames

        reviewer._extract_frames = types.MethodType(fake_extract, reviewer)
        timeline_payload = self.timeline()
        timeline_payload["candidate_directing"] = {"quality_gate_passed": False}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preview = root / "preview.mp4"
            preview.write_bytes(b"video")
            timeline = root / "timeline.json"
            timeline.write_text(json.dumps(timeline_payload), encoding="utf-8")
            result = reviewer.review(preview, timeline, root / "review.json")

        self.assertFalse(result["passes"])
        self.assertTrue(result["deterministic_failures"])
        self.assertTrue(result["blind_review"]["model_passes"])


if __name__ == "__main__":
    unittest.main()
