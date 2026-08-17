"""Standard-library tests for chunking and Ollama response handling."""

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.director import (
    AIDirector,
    ATOM_REFINEMENT_SCHEMA,
    CANDIDATE_SCHEMA,
    DirectorError,
    EditorialQualityError,
    EVIDENCE_ATOM_SCHEMA,
    build_evidence_fingerprint,
)


class FakeResponse:
    """Minimal requests.Response substitute."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))

    def json(self):
        return self.payload


class FakeSession:
    """Ollama session double recording unload requests."""

    def __init__(self):
        self.posts = []

    def get(self, url, timeout=None):
        if url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "test-model:latest"}]})
        if url.endswith("/api/ps"):
            return FakeResponse({"models": []})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if json.get("keep_alive") == 0:
            return FakeResponse({"done": True, "response": ""})
        generated = {
            "decisions": [
                {
                    "cut_in_sec": 10.0,
                    "cut_out_sec": 20.0,
                    "reason_for_cut": "完整观点",
                    "confidence": 0.9,
                }
            ]
        }
        return FakeResponse(
            {
                "done": True,
                "response": __import__("json").dumps(generated, ensure_ascii=False),
            }
        )


class VisionSession(FakeSession):
    """Two-pass multimodal Ollama double."""

    def get(self, url, timeout=None):
        if url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "qwen3.5:test"}]})
        if url.endswith("/api/ps"):
            return FakeResponse({"models": []})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if url.endswith("/api/show"):
            return FakeResponse({"capabilities": ["completion", "vision"]})
        if json.get("keep_alive") == 0:
            return FakeResponse({"done": True, "response": ""})
        prompt = json.get("prompt", "")
        if "STORY CONCEPT TOURNAMENT" in prompt:
            generated = {
                "concepts": [
                    {
                        "concept_id": "concept-a", "title": "Focused action",
                        "form": "character_vignette", "premise": "One action reveals preparation.",
                        "viewer_takeaway": "Preparation creates focus.",
                        "opening": "Enter the action.", "development": "Preparation becomes intent.",
                        "payoff": "The action reaches its apex.", "ending": "Hold the completed state.",
                        "proof_candidate_ids": ["C0001", "C0002"], "ending_candidate_id": "C0002",
                        "music_direction": "Restrained pulse.", "color_direction": "Warm controlled night.",
                        "feasibility_score": 9, "biggest_risk": "Only one observed atom.",
                        "recommended_duration_sec": 12,
                    },
                    {
                        "concept_id": "concept-b", "title": "Motion study",
                        "form": "kinetic_style_film", "premise": "A visual action becomes rhythm.",
                        "viewer_takeaway": "Movement has graphic force.",
                        "opening": "Begin on motion.", "development": "Repeat visual rhythm.",
                        "payoff": "Land on the action apex.", "ending": "Release on the exit state.",
                        "proof_candidate_ids": ["C0001", "C0002"], "ending_candidate_id": "C0002",
                        "music_direction": "Percussive instrumental.", "color_direction": "Clean contrast.",
                        "feasibility_score": 7, "biggest_risk": "Limited visual variety.",
                        "recommended_duration_sec": 10,
                    },
                    {
                        "concept_id": "concept-c", "title": "Quiet observation",
                        "form": "atmospheric_poem", "premise": "A small action carries atmosphere.",
                        "viewer_takeaway": "Attention transforms a simple moment.",
                        "opening": "Observe the entry.", "development": "Stay with the gesture.",
                        "payoff": "Reveal the apex.", "ending": "End on stillness.",
                        "proof_candidate_ids": ["C0001", "C0002"], "ending_candidate_id": "C0002",
                        "music_direction": "Sparse instrumental texture.", "color_direction": "Natural low-key tone.",
                        "feasibility_score": 6, "biggest_risk": "May feel too slight.",
                        "recommended_duration_sec": 10,
                    },
                ],
                "selected_concept_id": "concept-a",
                "selection_reason": "It communicates the clearest observed action.",
            }
        elif "Turn the winning evidence-backed concept" in prompt:
            generated = {
                "title": "Preparation to payoff",
                "logline": "A crew turns preparation into one precise action.",
                "central_theme": "coordination",
                "chronology_policy": "strict_chronological",
                "target_duration_sec": 45,
                "opening_beat": "Introduce the crew",
                "development_beat": "Build anticipation",
                "payoff_beat": "Complete the action",
                "ending_beat": "Hold on the result",
                "color_intent": "Natural skin with controlled contrast",
                "creative_look": "cinematic_warm",
                "music_mood": "restrained anticipation",
                "music_energy_arc": "quiet build to a clear finish",
                "editorial_rules": ["Preserve chronology", "Reject repetition"],
                "story_anchors": [
                    {
                        "asset_id": "asset-1",
                        "cut_in_sec": 0,
                        "cut_out_sec": 3,
                        "beat": "opening",
                        "reason": "Ground the opening",
                    },
                    {
                        "asset_id": "asset-1",
                        "cut_in_sec": 3,
                        "cut_out_sec": 5,
                        "beat": "payoff",
                        "reason": "Show the payoff",
                    },
                    {
                        "asset_id": "asset-1",
                        "cut_in_sec": 5,
                        "cut_out_sec": 8,
                        "beat": "ending",
                        "reason": "Complete the ending",
                    },
                ],
            }
        elif "CONTINUOUS FULL-FOOTAGE SYNTHESIS" in prompt:
            generated = {
                "whole_footage_summary": "The crew prepares and completes one coordinated action.",
                "discovered_central_theme": "Coordination turns preparation into payoff.",
                "character_threads": ["The crew gathers, prepares, and acts together."],
                "event_timeline": [
                    {
                        "asset_id": "asset-1", "source_order": 0,
                        "event": "Preparation leads to the final action.",
                        "story_meaning": "The payoff depends on shared coordination.",
                    }
                ],
                "visual_motifs": ["Repeated preparation gestures"],
                "continuity_risks": ["Do not repeat the same setup action"],
                "observed_ending": "The coordinated action is visibly completed.",
                "absent_or_unproven_events": [],
                "honest_adaptation": "Use the observed coordinated action as the ending.",
            }
        elif "EVIDENCE-FIRST STORY CONTRACT" in prompt:
            generated = {
                "narrative_mode": "character_vignette",
                "premise": "One observed action reveals the subject's preparation.",
                "subject": "The person preparing",
                "observed_goal": "Complete the observed action",
                "has_causal_arc": False,
                "causal_chain": [
                    {
                        "candidate_id": "C0001",
                        "observed_fact": "The person enters and begins an action.",
                        "state_before": "The frame is empty.",
                        "state_after": "The person is acting in frame.",
                        "story_consequence": "The subject is introduced.",
                        "evidence_type": "visual",
                    }
                ],
                "final_observed_state": "The observed action is complete.",
                "unsupported_promises": [],
                "dialogue_policy": "story_dialogue_only",
                "success_criteria": [
                    "The subject is clear.", "The action is readable.", "The ending is observed."
                ],
                "recommended_duration_sec": 12,
            }
        elif "Build one coherent documentary edit" in prompt:
            generated = {
                "project_summary": "A coherent multi-camera story",
                "sequence": [
                    {
                        "candidate_id": "C0001",
                        "reason_for_position": "Strong opening",
                        "transition_to_next": "cross_dissolve",
                        "transition_duration_sec": 0.5,
                        "audio_cleanup": "strong",
                        "color_look": "warm",
                        "motion": "gentle_push_in",
                    }
                ],
            }
        elif "SUPERVISING EDITOR REVIEW" in prompt:
            generated = {
                "project_summary": "A coherent multi-camera story",
                "viewer_takeaway": "Coordination creates the payoff.",
                "editorial_style": "narrative_documentary",
                "graphics_plan": {"strategy": "No graphics needed.", "items": []},
                "sequence": [
                    {
                        "candidate_id": "C0001",
                        "trim_in_sec": 1.0,
                        "trim_out_sec": 4.0,
                        "narrative_function": "hook",
                        "viewer_information": "A person begins the action.",
                        "reason_for_position": "Grounds the story in observed action.",
                        "evidence_claim": "A person enters and begins the action.",
                        "connection_to_previous": "Opens the film.",
                        "audio_intent": "preserve_dialogue",
                        "music_edit_role": "phrase_start",
                        "transition_to_next": "cross_dissolve",
                        "transition_duration_sec": 0.5,
                        "audio_cleanup": "strong",
                        "color_look": "warm",
                        "motion": "gentle_push_in",
                    }
                ],
                "review": {
                    "clarity_score": 8,
                    "pacing_score": 8,
                    "visual_storytelling_score": 8,
                    "rhythm_score": 7,
                    "problems_found": [
                        "The draft held the opening longer than its visible action required."
                    ],
                    "changes_made": [
                        "Tightened the opening to the exact observed action."
                    ],
                    "dialogue_strategy": "Keep only the line that changes viewer understanding.",
                    "rhythm_strategy": "Move from natural sound into a phrase start.",
                },
            }
        elif "BLIND VIEWER TEST" in prompt:
            generated = {
                "literal_synopsis": "A person enters and completes one observed action.",
                "subject": "The person",
                "apparent_goal": "Complete the action",
                "progression": ["The person enters", "The action begins", "The action ends"],
                "ending": "The action is visibly complete.",
                "takeaway_guess": "Preparation creates a focused moment.",
                "coherence_score": 8,
                "causal_clarity_score": 8,
                "visual_payoff_score": 8,
                "confusing_transitions": [],
                "unsupported_or_unresolved_points": [],
                "passes": True,
                "reason": "The subject and action are legible.",
            }
        elif "Design the final documentary music cue sheet" in prompt:
            generated = {
                "music_plan": {
                    "strategy": "One restrained cue under the payoff",
                    "silence_regions": [],
                    "cues": [],
                }
            }
        else:
            generated = {
                "continuity_summary": "A person enters and begins the source action.",
                "decisions": [
                    {
                        "cut_in_sec": 1.0,
                        "cut_out_sec": 4.0,
                        "reason_for_cut": "Clear visual opening",
                        "visual_summary": "Person enters the landscape",
                        "story_role": "opening",
                        "confidence": 0.9,
                        "quality_score": 0.95,
                        "transition_to_next": "cut",
                        "transition_duration_sec": 0,
                        "audio_cleanup": "light",
                        "color_look": "neutral",
                        "motion": "static",
                    }
                ]
            }
        return FakeResponse(
            {"done": True, "response": __import__("json").dumps(generated)}
        )


class EmptyThinkingSession(FakeSession):
    """Return an exhausted thinking-only response before valid direct JSON."""

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if json.get("keep_alive") == 0:
            return FakeResponse({"done": True, "response": ""})
        generation_posts = [
            item for item in self.posts if item.get("keep_alive") != 0
        ]
        if len(generation_posts) == 1:
            return FakeResponse(
                {
                    "done": True,
                    "done_reason": "length",
                    "prompt_eval_count": 4000,
                    "eval_count": 2048,
                    "thinking": "internal reasoning",
                    "response": "",
                }
            )
        return FakeResponse(
            {
                "done": True,
                "done_reason": "stop",
                "response": __import__("json").dumps({"decisions": []}),
            }
        )


class AIDirectorTests(unittest.TestCase):
    """Test deterministic behavior without a live Ollama server."""

    def make_director(self, **overrides):
        values = {
            "model": "test-model:latest",
            "chunk_minutes": 10,
            "session": FakeSession(),
            "logger": logging.getLogger("test.director"),
        }
        values.update(overrides)
        return AIDirector(**values)

    @staticmethod
    def raw_data(duration=1300.0):
        return {
            "duration_sec": duration,
            "source_video": "source.mp4",
            "proxy_file_name": "proxy.mp4",
            "transcript": [
                {"start_sec": 0.0, "end_sec": 10.0, "text": "a"},
                {"start_sec": 599.0, "end_sec": 603.0, "text": "b"},
                {"start_sec": 1201.0, "end_sec": 1205.0, "text": "c"},
            ],
            "keyframes": [
                {"timestamp_sec": 100.0, "file_name": "a.jpg"},
                {"timestamp_sec": 700.0, "file_name": "b.jpg"},
            ],
        }

    def test_chunking_assigns_each_segment_once(self):
        chunks = self.make_director().chunk_raw_data(self.raw_data())
        self.assertEqual(len(chunks), 3)
        texts = [
            segment["text"]
            for chunk in chunks
            for segment in chunk["transcript"]
        ]
        self.assertEqual(texts, ["a", "b", "c"])

    def test_visual_micro_scene_window_can_be_shorter_than_text_chunk(self):
        chunks = self.make_director().chunk_raw_data(
            self.raw_data(), window_sec=180
        )

        self.assertEqual(len(chunks), 3)
        self.assertTrue(
            all(chunk["end_sec"] - chunk["start_sec"] <= 180 for chunk in chunks)
        )

    def test_continuous_review_batches_overlap_without_losing_core_time(self):
        raw = {
            "duration_sec": 40.0,
            "transcript": [],
            "keyframes": [
                {"timestamp_sec": float(second), "file_name": f"{second}.jpg"}
                for second in range(40)
            ],
        }

        chunks = self.make_director().chunk_raw_data(
            raw, window_sec=16, overlap_sec=2
        )

        self.assertEqual(
            [(item["core_start_sec"], item["core_end_sec"]) for item in chunks],
            [(0.0, 16.0), (16.0, 32.0), (32.0, 40.0)],
        )
        self.assertEqual(
            [(item["start_sec"], item["end_sec"]) for item in chunks],
            [(0.0, 18.0), (14.0, 34.0), (30.0, 40.0)],
        )

    def test_full_review_request_sends_every_extracted_frame(self):
        session = VisionSession()
        director = self.make_director(
            model="qwen3.5:test", session=session, num_ctx=32768
        )
        with tempfile.TemporaryDirectory() as temporary:
            frame = Path(temporary) / "frame.jpg"
            frame.write_bytes(b"jpeg")
            chunk = {
                "index": 0,
                "start_sec": 0.0,
                "end_sec": 16.0,
                "core_start_sec": 0.0,
                "core_end_sec": 16.0,
                "source_order": 0,
                "continuity_context": "source begins",
                "transcript": [],
                "keyframes": [
                    {
                        "timestamp_sec": float(second),
                        "file_name": f"{second}.jpg",
                        "image_path": str(frame),
                    }
                    for second in range(16)
                ],
            }

            director.request_chunk(
                chunk,
                "source.mp4",
                schema=CANDIDATE_SCHEMA,
                include_images=True,
                treatment={},
            )

        request = next(item for item in session.posts if item.get("images"))
        self.assertEqual(len(request["images"]), 16)
        self.assertIn("CONTINUITY FROM PREVIOUS BATCH", request["prompt"])

    def test_neutral_visual_prompt_precedes_any_treatment(self):
        director = self.make_director()
        prompt = director.build_prompt(
            {
                "start_sec": 0.0, "end_sec": 10.0,
                "core_start_sec": 0.0, "core_end_sec": 10.0,
                "source_order": 0, "continuity_context": "source begins",
                "transcript": [], "keyframes": [],
            },
            "source.mp4",
            EVIDENCE_ATOM_SCHEMA,
            treatment=None,
        )

        self.assertIn("NEUTRAL EVIDENCE PASS", prompt)
        self.assertIn("entry_state", prompt)
        self.assertNotIn("Follow the DIRECTOR TREATMENT", prompt)
        self.assertNotIn("Suggest restrained effects", prompt)

    def test_neutral_schema_and_validator_cannot_make_creative_decisions(self):
        properties = EVIDENCE_ATOM_SCHEMA["properties"]["decisions"]["items"][
            "properties"
        ]
        for forbidden in (
            "reason_for_cut", "story_role", "transition_to_next", "color_look",
            "drx_preset", "stabilization", "tracking", "rhythmic_potential",
        ):
            self.assertNotIn(forbidden, properties)

        payload = {
            "continuity_summary": "A rider enters and raises one glove.",
            "decisions": [
                {
                    "cut_in_sec": 1.0,
                    "cut_out_sec": 9.8,
                    "visual_summary": "A rider enters, stops, and raises a glove.",
                    "subject_action": "The rider raises a glove after stopping.",
                    "observable_emotion": "focused",
                    "entry_state": "Rider enters frame.",
                    "action_apex": "Glove reaches shoulder height.",
                    "exit_state": "Rider holds the raised glove.",
                    "screen_direction": "right",
                    "identity_tags": ["rider-red-helmet"],
                    "temporal_phase": "development",
                    "shot_scale": "medium",
                    "camera_motion": "tracking",
                    "continuity_tags": ["continuous gesture"],
                    "technical_readability": "clear",
                    "confidence": 0.93,
                }
            ],
        }
        result = self.make_director().validate_chunk_decisions(
            payload,
            {
                "start_sec": 0.0,
                "end_sec": 10.0,
                "core_start_sec": 0.0,
                "core_end_sec": 10.0,
            },
            "source.mp4",
            neutral_evidence=True,
        )

        self.assertEqual(result[0]["cut_out_sec"], 9.8)
        self.assertEqual(result[0]["evidence_type"], "visual_atom")
        self.assertNotIn("story_role", result[0])
        self.assertNotIn("transition_to_next", result[0])
        self.assertNotIn("reason_for_cut", result[0])

    def test_long_transcript_segment_survives_visual_transport_windows(self):
        director = self.make_director()
        visual = [
            {
                "asset_id": "asset-1", "source_order": 0,
                "file_name": "source.mp4", "cut_in_sec": 8.0,
                "cut_out_sec": 12.0, "evidence_type": "visual_atom",
                "visual_summary": "speaker gestures", "subject_action": "gestures",
                "identity_tags": ["speaker-a"],
            }
        ]
        assets = [
            {
                "asset_id": "asset-1", "source_video": "source.mp4",
                "duration_sec": 60.0,
                "transcript": [
                    {
                        "start_sec": 5.0, "end_sec": 30.0,
                        "text": "One complete thought that crosses three transport windows.",
                    },
                    {
                        "start_sec": 40.0, "end_sec": 44.0,
                        "text": "A later speaker is heard outside the sampled visual atom.",
                    },
                ],
            }
        ]

        result = director._attach_complete_transcript_atoms(visual, assets)

        transcript = next(
            item for item in result if item.get("evidence_type") == "transcript_atom"
        )
        self.assertEqual((transcript["cut_in_sec"], transcript["cut_out_sec"]), (5.0, 30.0))
        self.assertTrue(transcript["has_dialogue"])
        later = next(
            item for item in result
            if item.get("evidence_type") == "transcript_atom"
            and item.get("cut_in_sec") == 40.0
        )
        self.assertEqual(later["identity_tags"], [])
        self.assertNotEqual(later["visual_summary"], "speaker gestures")

    def test_evidence_fingerprint_invalidates_source_or_model_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / "raw_data.json"
            raw_path.write_text('{"visual_sampling":{"requested_interval_sec":0.5}}', encoding="utf-8")
            first = build_evidence_fingerprint(raw_path, "vision:q4")
            self.assertEqual(first, build_evidence_fingerprint(raw_path, "vision:q4"))
            self.assertNotEqual(first, build_evidence_fingerprint(raw_path, "vision:q8"))
            raw_path.write_text('{"visual_sampling":{"requested_interval_sec":1.0}}', encoding="utf-8")
            self.assertNotEqual(first, build_evidence_fingerprint(raw_path, "vision:q4"))

    def test_evidence_fingerprint_tracks_source_proxy_and_jpeg_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            proxy = root / "proxy.mp4"
            frame = root / "frame.jpg"
            source.write_bytes(b"source-a")
            proxy.write_bytes(b"proxy-a")
            frame.write_bytes(b"jpeg-a")
            raw_path = root / "raw_data.json"
            raw_path.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "asset_id": "asset-1",
                                "source_video": str(source),
                                "proxy_file_name": str(proxy),
                                "keyframes": [{"image_path": str(frame)}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            previous = build_evidence_fingerprint(raw_path, "vision:q4")
            for evidence_file, suffix in (
                (source, b"-replaced"),
                (proxy, b"-replaced"),
                (frame, b"-replaced"),
            ):
                evidence_file.write_bytes(evidence_file.read_bytes() + suffix)
                current = build_evidence_fingerprint(raw_path, "vision:q4")
                self.assertNotEqual(previous, current)
                previous = current

            frame.unlink()
            with self.assertRaisesRegex(DirectorError, "Missing or unreadable"):
                build_evidence_fingerprint(raw_path, "vision:q4")

    def test_compact_treatment_excludes_hierarchical_evidence_pages(self):
        sentinel = "FULL_LEDGER_PAGE_SHOULD_NOT_REENTER_PROMPTS"
        treatment = {
            "title": "A focused film",
            "central_theme": "Preparation becomes motion",
            "selected_concept_id": "concept-b",
            "concept_tournament": {
                "selected_concept_id": "concept-b",
                "selection_reason": "The ending is directly observable.",
                "concepts": [
                    {"concept_id": "concept-a", "premise": "Unused"},
                    {
                        "concept_id": "concept-b",
                        "premise": "A rider prepares and leaves.",
                        "proof_candidate_ids": ["C0001", "C0008"],
                    },
                ],
                "evidence_review": {
                    "pages": [{"page_summary": sentinel * 1000}]
                },
            },
            "story_anchors": [],
            "footage_ledger": "C:/private/footage_ledger.json",
            "evidence_fingerprint": "durable-only",
        }

        compact = AIDirector._compact_treatment_for_prompt(treatment)
        encoded = json.dumps(compact, ensure_ascii=False)

        self.assertNotIn(sentinel, encoded)
        self.assertNotIn("evidence_review", encoded)
        self.assertNotIn("footage_ledger", compact)
        self.assertNotIn("evidence_fingerprint", compact)
        self.assertEqual(
            compact["concept_tournament"]["selected_concept"]["concept_id"],
            "concept-b",
        )

    def test_treatment_fingerprint_is_required_and_must_match(self):
        expected = "current-evidence"
        with self.assertRaisesRegex(DirectorError, "evidence fingerprint"):
            AIDirector._require_treatment_evidence_fingerprint({}, expected)
        with self.assertRaisesRegex(DirectorError, "evidence fingerprint"):
            AIDirector._require_treatment_evidence_fingerprint(
                {"evidence_fingerprint": "stale-evidence"}, expected
            )
        AIDirector._require_treatment_evidence_fingerprint(
            {"evidence_fingerprint": expected}, expected
        )

    def test_check_ollama_rejects_resident_variant_with_same_base_name(self):
        class SameBaseVariantSession(FakeSession):
            def get(self, url, timeout=None):
                if url.endswith("/api/ps"):
                    return FakeResponse(
                        {"models": [{"name": "test-model:other-quant"}]}
                    )
                return super().get(url, timeout=timeout)

        director = self.make_director(session=SameBaseVariantSession())
        with self.assertRaisesRegex(DirectorError, "Other Ollama models"):
            director.check_ollama()

    def test_assign_missing_candidate_ids_preserves_audit_ids(self):
        candidates = [
            {"candidate_id": "C0042"},
            {"candidate_origin": "treatment_fallback"},
        ]

        AIDirector._assign_missing_candidate_ids(candidates, prefix="R")

        self.assertEqual(candidates[0]["candidate_id"], "C0042")
        self.assertEqual(candidates[1]["candidate_id"], "R0001")

    def test_event_atom_deduplication_never_merges_adjacent_actions(self):
        common = {
            "asset_id": "a", "source_order": 0, "file_name": "a.mp4",
            "quality_score": 0.8, "confidence": 0.8,
        }
        result = AIDirector._deduplicate_event_atoms([
            {
                **common, "cut_in_sec": 0.0, "cut_out_sec": 2.0,
                "subject_action": "rider puts on a glove",
            },
            {
                **common, "cut_in_sec": 1.8, "cut_out_sec": 3.5,
                "subject_action": "rider fastens the helmet",
            },
            {
                **common, "cut_in_sec": 0.1, "cut_out_sec": 2.1,
                "subject_action": "rider puts on the glove",
                "quality_score": 0.9,
            },
        ])

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["quality_score"], 0.9)
        self.assertIn("helmet", result[1]["subject_action"])

    def test_dense_temporal_refinement_preserves_atoms_when_ffmpeg_is_missing(self):
        director = self.make_director()
        candidates = [{
            "asset_id": "asset-1",
            "cut_in_sec": 1.0,
            "cut_out_sec": 3.0,
            "subject_action": "rider raises a helmet",
        }]

        with patch("src.director.shutil.which", return_value=None):
            refined, rejected = director._refine_event_atoms_temporally(
                candidates, [{"asset_id": "asset-1", "duration_sec": 10.0}]
            )

        self.assertEqual(rejected, [])
        self.assertEqual(len(refined), 1)
        self.assertEqual(refined[0]["cut_in_sec"], 1.0)
        self.assertEqual(
            refined[0]["temporal_refinement"]["status"], "ffmpeg_unavailable"
        )

    def test_hierarchical_story_seed_audit_accounts_for_every_atom(self):
        director = self.make_director()
        evidence = [
            {
                "candidate_id": f"C{index:04d}",
                "asset_id": "asset-1",
                "source_order": 0,
                "in": float(index),
                "out": float(index + 1),
                "literal_visual": f"action {index}",
            }
            for index in range(1, 4)
        ]
        response = {
            "page_summary": "Three consecutive observed actions.",
            "story_seeds": [{
                "form": "character_vignette",
                "premise": "A person completes a sequence.",
                "proof_candidate_ids": ["C0001", "C0002", "C0003"],
                "possible_ending_candidate_id": "C0003",
                "observable_progression": "The state changes three times.",
                "limitations": "Only one location is visible.",
            }],
        }

        with patch.object(director, "_request_json", return_value=response):
            audit = director._synthesize_story_seed_pages(evidence, [])

        self.assertTrue(audit["all_candidates_considered"])
        self.assertEqual(audit["input_candidate_count"], 3)
        self.assertEqual(
            audit["pages"][0]["input_candidate_ids"],
            ["C0001", "C0002", "C0003"],
        )

    def test_story_seed_pages_use_bounded_asset_coverage_at_72b_context(self):
        sentinel = "ROLLING_REVIEW_CONCLUSION_MUST_STAY_ON_DISK"
        director = self.make_director(
            text_model="qwen3.6:72b-q5", num_ctx=32768
        )
        coverage = [
            {
                "asset_id": f"asset-{index:02d}",
                "source_order": index,
                "file": f"C{1900 + index}.MP4",
                "duration_sec": 120.0,
                "saved_visual_samples": 240,
                "candidate_atom_count": 20,
                "disposition": "event_atoms_recorded",
                "review_conclusion": sentinel + ("x" * 800),
            }
            for index in range(18)
        ]
        evidence = [
            {
                "candidate_id": "C0001",
                "asset_id": "asset-00",
                "source_order": 0,
                "in": 1.0,
                "out": 2.0,
                "literal_visual": "A rider closes a helmet visor.",
            }
        ]
        prompts = []

        def request(prompt, schema, images=(), model=None, progress_activity=""):
            del schema, images, model, progress_activity
            prompts.append(prompt)
            return {
                "page_summary": "A rider completes one observed action.",
                "story_seeds": [
                    {
                        "form": "character_vignette",
                        "premise": "Preparation becomes a completed gesture.",
                        "proof_candidate_ids": ["C0001"],
                        "possible_ending_candidate_id": "C0001",
                        "observable_progression": "The visor moves from open to closed.",
                        "limitations": "Only one action is present.",
                    }
                ],
            }

        with patch.object(director, "_request_json", side_effect=request):
            audit = director._synthesize_story_seed_pages(evidence, coverage)

        self.assertEqual(audit["input_candidate_count"], 1)
        self.assertEqual(len(prompts), 1)
        self.assertNotIn(sentinel, prompts[0])
        self.assertIn('"total_sources":18', prompts[0])

    def test_concept_fixed_prefix_fails_before_expensive_story_pages(self):
        director = self.make_director(
            text_model="qwen3.6:72b-q5",
            num_ctx=32768,
            creative_brief="very long brief " * 5000,
        )
        candidates = [
            {
                "candidate_id": "C0001",
                "asset_id": "asset-1",
                "source_order": 0,
                "cut_in_sec": 0.0,
                "cut_out_sec": 1.0,
                "visual_summary": "One observed action.",
            }
        ]

        with patch.object(
            director,
            "_synthesize_story_seed_pages",
        ) as synthesize:
            with self.assertRaisesRegex(DirectorError, "fixed concept prefix"):
                director.request_story_concepts([], candidates, [])

        synthesize.assert_not_called()

    def test_visual_review_window_adapts_to_image_token_budget(self):
        director = self.make_director(
            model="qwen3.6:27b", num_ctx=32768
        )
        asset = {
            "asset_id": "asset-1",
            "source_video": "source.mp4",
            "proxy_file_name": "source.mp4",
            "duration_sec": 20.0,
            "transcript": [],
            "keyframes": [
                {
                    "timestamp_sec": index * 0.5,
                    "file_name": f"frame-{index:03d}.jpg",
                }
                for index in range(40)
            ],
        }

        chunks = director._build_visual_review_chunks([asset])
        budget = director._vision_image_budget(EVIDENCE_ATOM_SCHEMA)

        self.assertLess(
            max(chunk["core_end_sec"] - chunk["core_start_sec"] for chunk in chunks),
            10.0,
        )
        self.assertTrue(
            all(len(chunk["keyframes"]) <= budget for chunk in chunks)
        )
        self.assertEqual(chunks[0]["core_start_sec"], 0.0)
        self.assertEqual(chunks[-1]["core_end_sec"], 20.0)
        self.assertEqual(
            [chunk["core_end_sec"] for chunk in chunks[:-1]],
            [chunk["core_start_sec"] for chunk in chunks[1:]],
        )
        for keyframe in asset["keyframes"]:
            timestamp = keyframe["timestamp_sec"]
            self.assertTrue(
                any(
                    chunk["core_start_sec"] <= timestamp < chunk["core_end_sec"]
                    or (
                        chunk is chunks[-1]
                        and timestamp == chunk["core_end_sec"]
                    )
                    for chunk in chunks
                ),
                f"keyframe {timestamp} is outside every adaptive core",
            )

    def test_story_seed_reduction_recurses_without_prompting_page_provenance(self):
        director = self.make_director()
        original_ids = {f"C{index:04d}" for index in range(1, 7)}
        nodes = [
            {
                "page_summary": f"Observed action {index}.",
                "story_seeds": [
                    {
                        "form": "character_vignette",
                        "premise": f"Action {index} changes the state.",
                        "proof_candidate_ids": [f"C{index:04d}"],
                        "possible_ending_candidate_id": f"C{index:04d}",
                        "observable_progression": "A visible state changes.",
                        "limitations": "Only one action is represented.",
                    }
                ],
            }
            for index in range(1, 7)
        ]

        def capacity(prompt, schema, model=None, reserve_output_tokens=0):
            del schema, model, reserve_output_tokens
            return prompt.count('"page_summary"') <= 2

        def reduce_request(
            prompt,
            schema,
            images=(),
            model=None,
            progress_activity="director_generation",
        ):
            del schema, images, model, progress_activity
            group = json.loads(prompt.split("SEED NODES: ", 1)[1])
            proof_ids = list(dict.fromkeys(
                candidate_id
                for node in group
                for seed in node["story_seeds"]
                for candidate_id in seed["proof_candidate_ids"]
            ))
            return {
                "page_summary": "All supplied nodes were merged chronologically.",
                "story_seeds": [
                    {
                        "form": "character_vignette",
                        "premise": "The observed actions form one progression.",
                        "proof_candidate_ids": proof_ids,
                        "possible_ending_candidate_id": proof_ids[-1],
                        "observable_progression": "Each action changes the visible state.",
                        "limitations": "No events outside the supplied evidence are claimed.",
                    }
                ],
            }

        director._request_has_capacity = capacity
        director._request_json = reduce_request
        audits = []
        level = 1
        while len(nodes) > 1:
            nodes, audit = director._reduce_story_seed_nodes(nodes, level)
            audits.append(audit)
            level += 1

        final_ids = set(nodes[0]["story_seeds"][0]["proof_candidate_ids"])
        self.assertEqual(final_ids, original_ids)
        self.assertGreaterEqual(len(audits), 2)
        self.assertTrue(all(
            audit["output_node_count"] < audit["input_node_count"]
            for audit in audits
        ))

        review = {
            "all_candidates_considered": True,
            "input_candidate_count": 6,
            "page_count": 1,
            "pages": [
                {
                    "input_candidate_ids": ["PROVENANCE_ONLY_SENTINEL"],
                    "page_summary": "Observed actions.",
                    "story_seeds": nodes[0]["story_seeds"],
                }
            ],
        }
        prompt_view = AIDirector._story_seed_prompt_view(review)
        self.assertNotIn(
            "PROVENANCE_ONLY_SENTINEL",
            json.dumps(prompt_view, ensure_ascii=False),
        )

    def test_treatment_excerpt_samples_start_middle_and_end_within_budget(self):
        segments = [
            {"start_sec": index * 10, "end_sec": index * 10 + 5, "text": f"line-{index} " + "x" * 40}
            for index in range(20)
        ]

        excerpt, count = AIDirector._compact_transcript_excerpt(segments, 360)

        self.assertEqual(count, 20)
        self.assertLessEqual(len(excerpt), 361)
        self.assertIn("line-0", excerpt)
        self.assertIn("line-19", excerpt)
        self.assertTrue(any(f"line-{index}" in excerpt for index in range(7, 14)))

    def test_merge_overlap_and_reason(self):
        merged = self.make_director().merge_decisions(
            [
                {
                    "file_name": "proxy.mp4",
                    "cut_in_sec": 1.0,
                    "cut_out_sec": 5.0,
                    "reason_for_cut": "A",
                    "confidence": 0.9,
                },
                {
                    "file_name": "proxy.mp4",
                    "cut_in_sec": 5.2,
                    "cut_out_sec": 8.0,
                    "reason_for_cut": "B",
                    "confidence": 0.8,
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["cut_out_sec"], 8.0)
        self.assertEqual(merged[0]["reason_for_cut"], "A；B")
        self.assertEqual(merged[0]["confidence"], 0.8)

    def test_merge_respects_dynamic_broll_duration(self):
        merged = self.make_director().merge_decisions(
            [
                {
                    "file_name": "proxy.mp4", "cut_in_sec": 0.0,
                    "cut_out_sec": 10.0, "reason_for_cut": "A", "confidence": 0.9,
                    "story_role": "broll",
                },
                {
                    "file_name": "proxy.mp4", "cut_in_sec": 10.1,
                    "cut_out_sec": 20.0, "reason_for_cut": "B", "confidence": 0.9,
                    "story_role": "broll",
                },
            ]
        )

        self.assertEqual(len(merged), 2)

    def test_story_coverage_does_not_force_every_source_into_the_film(self):
        director = self.make_director()
        candidates = []
        roles = ["opening", "context", "climax", "closing", "context", "context"]
        summaries = [
            "An empty road establishes the location",
            "Friends unload motorcycles and prepare helmets",
            "The engines start and the group launches",
            "Tail lights disappear into the night",
            "A rider tightens a glove before departure",
            "Wide skyline reveals the scale of the route",
        ]
        for index, role in enumerate(roles):
            candidates.append(
                {
                    "candidate_id": f"C{index + 1:04d}",
                    "source_order": index,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 3.0,
                    "story_role": role,
                    "visual_summary": summaries[index],
                    "reason_for_cut": summaries[index],
                    "quality_score": 0.8,
                    "confidence": 0.8,
                }
            )

        result = director._complete_story_coverage(
            [candidates[0], candidates[2], candidates[3]],
            candidates,
            {"target_duration_sec": 10.0, "chronology_policy": "strict_chronological"},
        )

        self.assertLess(len({item["source_order"] for item in result}), len(candidates))
        self.assertEqual(
            {director._canonical_story_beat(item) for item in result},
            {"opening", "development", "payoff", "ending"},
        )

    def test_semantically_repeated_countdowns_are_collapsed(self):
        director = self.make_director()
        clips = [
            {
                "candidate_id": "C0001",
                "visual_summary": "Riders are geared up; a voice counts 3 2 1 before engines rev",
                "reason_for_cut": "The group prepares to launch", "source_order": 1,
            },
            {
                "candidate_id": "C0002", "visual_summary": "The final cue is 3 2 1 Start",
                "reason_for_cut": "Definitive action ending", "source_order": 2,
            },
        ]

        self.assertEqual(len(director._remove_semantic_redundancy(clips)), 1)

    def test_global_creative_look_is_uniform_across_selected_clips(self):
        director = self.make_director()
        candidates = [
            {
                "candidate_id": "C0001", "asset_id": "a", "source_order": 0,
                "file_name": "a.mp4", "cut_in_sec": 0.0, "cut_out_sec": 3.0,
                "story_role": "opening", "visual_summary": "The group gathers",
                "reason_for_cut": "Opening", "color_look": "cool",
            },
            {
                "candidate_id": "C0002", "asset_id": "b", "source_order": 1,
                "file_name": "b.mp4", "cut_in_sec": 0.0, "cut_out_sec": 3.0,
                "story_role": "closing", "visual_summary": "The group leaves",
                "reason_for_cut": "Ending", "color_look": "contrast",
            },
        ]
        sequence = {
            "sequence": [
                {"candidate_id": "C0001", "reason_for_position": "start"},
                {"candidate_id": "C0002", "reason_for_position": "finish"},
            ]
        }
        treatment = {
            "creative_look": "cinematic_warm", "target_duration_sec": 6.0,
            "chronology_policy": "strict_chronological",
        }

        result = director.validate_sequence(sequence, candidates, treatment)

        self.assertEqual({item["color_look"] for item in result}, {"warm"})
        self.assertEqual(result[0]["creative_grade"]["story_beat"], "opening")
        self.assertEqual(result[-1]["creative_grade"]["story_beat"], "ending")

    def test_color_bible_is_clamped_and_applied_by_story_beat(self):
        director = self.make_director()
        bible = director._validate_color_bible(
            {
                "global_palette": "neon_night",
                "contrast": 99,
                "saturation": 0,
                "warmth": -9,
                "highlight_rolloff": 2,
                "chapter_grades": [
                    {
                        "beat": "payoff", "exposure_ev": 2,
                        "contrast": 2, "saturation": 2, "warmth": 2,
                        "reason": "Release the energy",
                    }
                ],
            },
            "cool_steel",
        )

        self.assertEqual(bible["global_palette"], "neon_night")
        self.assertEqual(bible["contrast"], 1.25)
        self.assertEqual(bible["saturation"], 0.75)
        self.assertEqual(bible["warmth"], -1.0)
        self.assertEqual(len(bible["chapter_grades"]), 4)
        graded = director._apply_creative_grade_plan(
            [{"story_role": "climax"}],
            {"creative_look": "cool_steel", "color_bible": bible},
        )
        self.assertEqual(graded[0]["creative_grade"]["story_beat"], "payoff")
        self.assertLessEqual(graded[0]["creative_grade"]["contrast"], 1.35)

    def test_parse_fenced_json(self):
        parsed = AIDirector.parse_generated_json(
            '```json\n{"decisions": []}\n```'
        )
        self.assertEqual(parsed, {"decisions": []})

    def test_visual_cut_snaps_to_nearby_beat_but_interview_does_not(self):
        director = self.make_director()
        clips = [
            {
                "asset_id": "a", "story_role": "broll", "cut_in_sec": 0.0,
                "cut_out_sec": 2.9,
                "reviewed_trim_bounds": {"in_sec": 0.0, "out_sec": 3.2},
            },
            {
                "asset_id": "a", "story_role": "interview", "cut_in_sec": 3.0,
                "cut_out_sec": 6.0,
            },
        ]
        result = director.snap_visual_cuts_to_beats(
            clips,
            {"beats_sec": [3.0, 6.0], "duration_sec": 10.0},
            [{"asset_id": "a", "duration_sec": 20.0}],
        )

        self.assertEqual(result[0]["cut_out_sec"], 3.0)
        self.assertIn("beat_snap", result[0])
        self.assertEqual(result[1]["cut_out_sec"], 6.0)
        self.assertNotIn("beat_snap", result[1])

    def test_beat_snap_stays_inside_reviewed_handle_and_after_action_apex(self):
        director = self.make_director()
        plan = {"beats_sec": [3.0]}
        asset = [{"asset_id": "a", "duration_sec": 20.0, "transcript": []}]

        outside_handle = director.snap_visual_cuts_to_beats(
            [{
                "asset_id": "a", "story_role": "broll", "cut_in_sec": 0.0,
                "cut_out_sec": 2.9,
                "reviewed_trim_bounds": {"in_sec": 0.0, "out_sec": 2.95},
            }],
            plan,
            asset,
        )
        before_apex = director.snap_visual_cuts_to_beats(
            [{
                "asset_id": "a", "story_role": "broll", "cut_in_sec": 0.0,
                "cut_out_sec": 2.9, "action_apex_sec": 3.05,
                "reviewed_trim_bounds": {"in_sec": 0.0, "out_sec": 3.2},
            }],
            plan,
            asset,
        )

        self.assertEqual(outside_handle[0]["cut_out_sec"], 2.9)
        self.assertNotIn("beat_snap", outside_handle[0])
        self.assertEqual(before_apex[0]["cut_out_sec"], 2.9)
        self.assertNotIn("beat_snap", before_apex[0])

    def test_beat_snap_does_not_extend_a_silent_shot_into_dialogue(self):
        director = self.make_director()
        result = director.snap_visual_cuts_to_beats(
            [{
                "asset_id": "a", "story_role": "broll", "cut_in_sec": 0.0,
                "cut_out_sec": 2.9,
                "reviewed_trim_bounds": {"in_sec": 0.0, "out_sec": 3.2},
            }],
            {"beats_sec": [3.0]},
            [{
                "asset_id": "a", "duration_sec": 20.0,
                "transcript": [{"start_sec": 2.95, "end_sec": 3.4, "text": "hello"}],
            }],
        )

        self.assertEqual(result[0]["cut_out_sec"], 2.9)
        self.assertNotIn("beat_snap", result[0])

    def test_priority_only_music_landmark_can_drive_a_safe_snap(self):
        director = self.make_director()
        result = director.snap_visual_cuts_to_beats(
            [{
                "asset_id": "a", "story_role": "broll", "cut_in_sec": 0.0,
                "cut_out_sec": 2.9, "music_edit_role": "on_beat",
                "reviewed_trim_bounds": {"in_sec": 0.0, "out_sec": 3.2},
            }],
            {
                "cues": [{
                    "timeline_in_sec": 0.0, "track_in_sec": 0.0,
                    "track_out_sec": 4.0, "beats_sec": [],
                    "strong_beats_sec": [], "downbeats_sec": [],
                    "sync_points": [{"timeline_sec": 3.0, "type": "section"}],
                }]
            },
            [{"asset_id": "a", "duration_sec": 20.0, "transcript": []}],
        )

        self.assertEqual(result[0]["cut_out_sec"], 3.0)
        self.assertEqual(result[0]["beat_snap"]["timeline_beat_sec"], 3.0)

    def test_sparse_music_sync_points_are_grounded_from_picture_roles(self):
        director = self.make_director()
        clips = [
            {"cut_in_sec": 0, "cut_out_sec": 3.1, "music_edit_role": "phrase_start"},
            {"cut_in_sec": 0, "cut_out_sec": 3.0, "music_edit_role": "payoff_hit"},
            {"cut_in_sec": 0, "cut_out_sec": 2.0, "music_edit_role": "release"},
        ]
        plan = director.enrich_music_sync_points(
            clips,
            {
                "cues": [
                    {
                        "timeline_in_sec": 0, "timeline_out_sec": 8,
                        "track_in_sec": 0, "track_out_sec": 8,
                        "downbeats_sec": [0, 3, 6],
                        "strong_beats_sec": [1, 2, 4, 5, 7],
                        "sync_points": [],
                    }
                ]
            },
        )

        self.assertGreaterEqual(len(plan["cues"][0]["sync_points"]), 2)
        self.assertGreaterEqual(plan["rhythm_audit"]["grounded_sync_points_added"], 2)

    def test_multi_cue_music_plan_is_bounded_to_analyzed_candidates(self):
        director = self.make_director()
        director._music_analysis = {
            "tracks": [
                {
                    "file_name": str(Path("authorized.wav").resolve()),
                    "duration_sec": 30,
                    "tempo_bpm": 100,
                    "strong_beats_sec": [1.0, 2.0, 3.0],
                    "license": "user-confirmed rights",
                }
            ]
        }
        director._music_files = [Path("authorized.wav").resolve()]

        plan = director.validate_music_plan(
            {
                "strategy": "build then breathe",
                "silence_regions": [],
                "cues": [
                    {
                        "cue_id": "M1",
                        "track_file": "authorized.wav",
                        "story_beat": "opening",
                        "timeline_in_sec": 0,
                        "timeline_out_sec": 8,
                        "track_in_sec": 1,
                        "track_out_sec": 9,
                        "target_lufs": -24,
                        "duck_under_dialogue_db": -9,
                    },
                    {"cue_id": "bad", "track_file": "invented.wav"},
                ],
            },
            program_duration_sec=10,
        )

        self.assertEqual(len(plan["cues"]), 1)
        self.assertEqual(plan["cues"][0]["track_file"], "authorized.wav")
        self.assertEqual(plan["mode"], "multi_cue_pre_mix")

    def test_music_quality_gate_rejects_flat_low_payoff_and_adjacent_duplicate(self):
        plan = {
            "cues": [
                {"cue_id": "M1", "file_name": "ambient.wav", "story_beat": "development",
                 "timeline_in_sec": 0, "timeline_out_sec": 10, "reason": "quiet opening",
                 "energy_profile": {"trend": "flat", "build_score": 0.1, "contrast_db": 1.0},
                 "sections": [{"energy": "low"}]},
                {"cue_id": "M2", "file_name": "ambient.wav", "story_beat": "payoff",
                 "timeline_in_sec": 10, "timeline_out_sec": 20, "reason": "rising climax",
                 "energy_profile": {"trend": "flat", "build_score": 0.1, "contrast_db": 1.0},
                 "sections": [{"energy": "low"}]},
            ]
        }
        violations = AIDirector.music_plan_quality_violations(
            plan, {"music_energy_arc": "slow rise into climax"}
        )
        self.assertTrue(any("all-low-energy" in item for item in violations))
        self.assertTrue(any("same track" in item for item in violations))

    def test_empty_thinking_response_retries_without_qwen_thinking(self):
        session = EmptyThinkingSession()
        director = self.make_director(
            model="qwen3.5:35b-a3b", session=session
        )

        result = director._request_json(
            "Return an empty decisions array.",
            {
                "type": "object",
                "properties": {"decisions": {"type": "array"}},
                "required": ["decisions"],
            },
        )

        self.assertEqual(result, {"decisions": []})
        generation_posts = [
            item for item in session.posts if item.get("keep_alive") != 0
        ]
        self.assertEqual(len(generation_posts), 2)
        self.assertIs(generation_posts[0]["think"], True)
        self.assertIs(generation_posts[1]["think"], False)
        self.assertEqual(
            generation_posts[0]["options"]["num_predict"], 2048
        )

    def test_qwen36_structured_json_disables_thinking_on_first_attempt(self):
        session = FakeSession()
        director = self.make_director(
            model="qwen3.6:27b-mtp-q8_0", session=session
        )

        result = director._request_json(
            "Return one decision.",
            {
                "type": "object",
                "properties": {"decisions": {"type": "array"}},
                "required": ["decisions"],
            },
        )

        self.assertIn("decisions", result)
        generation_posts = [
            item for item in session.posts if item.get("keep_alive") != 0
        ]
        self.assertEqual(len(generation_posts), 1)
        self.assertIs(generation_posts[0]["think"], False)

    def test_qwen38_structured_json_disables_thinking_on_literal_first_attempt(self):
        session = FakeSession()
        director = self.make_director(
            model="hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0",
            session=session,
        )

        result = director._request_json(
            "Return one decision.",
            {
                "type": "object",
                "properties": {"decisions": {"type": "array"}},
                "required": ["decisions"],
            },
        )

        self.assertIn("decisions", result)
        generation_posts = [
            item for item in session.posts if item.get("keep_alive") != 0
        ]
        self.assertEqual(len(generation_posts), 1)
        self.assertIs(generation_posts[0]["think"], False)

    def test_final_director_splits_picture_and_music_contexts(self):
        session = VisionSession()
        director = self.make_director(session=session)
        director._active_target_duration_sec = 12.0
        director._active_treatment = {
            "title": "A short ride",
            "central_theme": "preparation and release",
            "target_duration_sec": 12.0,
            "story_anchors": [],
        }
        director._music_analysis = {
            "tracks": [
                {
                    "file_name": "authorized.wav",
                    "title": "Quiet Motion",
                    "duration_sec": 60.0,
                    "tempo_bpm": 90.0,
                    "strong_beats_sec": [float(index) for index in range(100)],
                    "downbeats_sec": [float(index * 2) for index in range(50)],
                    "sections": [
                        {"start_sec": 0.0, "end_sec": 30.0, "energy": "low"},
                        {"start_sec": 30.0, "end_sec": 60.0, "energy": "high"},
                    ],
                }
            ]
        }
        candidates = [
            {
                "candidate_id": "C0001",
                "asset_id": "asset-1",
                "source_order": 0,
                "file_name": "source.mp4",
                "cut_in_sec": 1.0,
                "cut_out_sec": 4.0,
                "story_role": "opening",
                "visual_summary": "A rider prepares the motorcycle.",
                "reason_for_cut": "Clear visual opening.",
            }
        ]
        assets = [
            {"asset_id": "asset-1", "source_video": "source.mp4", "duration_sec": 8.0}
        ]

        result = director.request_sequence(
            candidates, assets, director._active_treatment
        )

        generation_posts = [
            item for item in session.posts if item.get("keep_alive") != 0
        ]
        self.assertEqual(len(generation_posts), 6)
        self.assertIn(
            "CONTINUOUS FULL-FOOTAGE SYNTHESIS", generation_posts[0]["prompt"]
        )
        self.assertIn(
            "EVIDENCE-FIRST STORY CONTRACT",
            generation_posts[1]["prompt"],
        )
        self.assertIn("step 1", generation_posts[2]["prompt"].casefold())
        self.assertIn("SUPERVISING EDITOR REVIEW", generation_posts[3]["prompt"])
        self.assertIn("BLIND VIEWER TEST", generation_posts[4]["prompt"])
        self.assertIn(
            "Design the final documentary music cue sheet",
            generation_posts[5]["prompt"],
        )
        self.assertNotIn("AVAILABLE MUSIC", generation_posts[0]["prompt"])
        self.assertEqual(result["sequence"][0]["candidate_id"], "C0001")
        self.assertIn("music_plan", result)
        self.assertTrue(result["candidate_directing"]["supervising_editor_reviewed"])
        self.assertTrue(result["candidate_directing"]["supervising_editor_changed_plan"])
        self.assertEqual(
            result["candidate_directing"]["supervising_review"]["clarity_score"],
            8,
        )
        self.assertEqual(result["candidate_directing"]["final_metrics"]["clip_count"], 1)

    def test_measured_quality_gate_sends_bad_plan_back_to_ai_for_recut(self):
        director = self.make_director(num_ctx=16384)
        director._active_target_duration_sec = 30.0
        treatment = {
            "central_theme": "Movement becomes collective focus.",
            "edit_style": "kinetic_montage",
            "target_duration_sec": 30.0,
        }
        candidates = []
        assets = []
        for index in range(8):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "asset_id": f"asset-{index}",
                    "source_order": index,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 6.0,
                    "story_role": "broll",
                    "visual_summary": f"Distinct observed action {index}",
                    "subject_action": f"Subject moves {index}",
                    "camera_motion": "static" if index < 6 else "tracking",
                    "shot_scale": "wide" if index < 6 else "closeup",
                    "has_dialogue": index < 5,
                }
            )
            assets.append(
                {
                    "asset_id": f"asset-{index}",
                    "source_video": f"source-{index}.mp4",
                    "duration_sec": 6.0,
                    "transcript": [],
                    "keyframes": [],
                }
            )

        def shot(candidate_id, function, music_role, audio="natural_texture"):
            return {
                "candidate_id": candidate_id,
                "trim_in_sec": 0.0,
                "trim_out_sec": 3.0,
                "narrative_function": function,
                "viewer_information": f"New information from {candidate_id}",
                "reason_for_position": f"Advances the cut with {candidate_id}",
                "evidence_claim": f"Observed action in {candidate_id}",
                "connection_to_previous": "Opens." if candidate_id == "C0001" else "Progresses visibly.",
                "audio_intent": audio,
                "music_edit_role": music_role,
                "transition_to_next": "cut",
                "transition_duration_sec": 0.0,
                "audio_cleanup": "none",
                "color_look": "neutral",
                "motion": "static",
            }

        bad_sequence = [
            shot(
                f"C{index + 1:04d}",
                "hook" if index == 0 else "closure" if index == 5 else "context",
                "payoff_hit" if index in {0, 5} else "build",
                "preserve_dialogue" if index < 5 else "natural_texture",
            )
            for index in range(6)
        ]
        good_sequence = [
            shot("C0001", "hook", "natural_sound"),
            shot("C0007", "escalation", "phrase_start"),
            shot("C0008", "payoff", "payoff_hit"),
            shot("C0006", "closure", "release"),
        ]
        calls = []

        def plan(sequence, include_review):
            payload = {
                "project_summary": "A concise movement study",
                "viewer_takeaway": "Small actions build collective focus.",
                "editorial_style": "kinetic_montage",
                "graphics_plan": {"strategy": "No graphics", "items": []},
                "sequence": sequence,
            }
            if include_review:
                payload["review"] = {
                    "clarity_score": 8,
                    "pacing_score": 8,
                    "visual_storytelling_score": 8,
                    "rhythm_score": 8,
                    "problems_found": ["The measured draft needed material revision."],
                    "changes_made": ["Changed selection, trims, and rhythm."],
                    "dialogue_strategy": "Use visual action instead of routine speech.",
                    "rhythm_strategy": "Progress from natural sound to payoff and release.",
                }
            return payload

        def fake_request(
            prompt, schema, images=(), model=None, progress_activity="director_generation"
        ):
            calls.append(progress_activity)
            if "CONTINUOUS FULL-FOOTAGE SYNTHESIS" in prompt:
                return {
                    "whole_footage_summary": "Several distinct actions occur.",
                    "discovered_central_theme": "Collective focus",
                    "character_threads": [],
                    "event_timeline": [],
                    "visual_motifs": [],
                    "continuity_risks": [],
                    "observed_ending": "The group settles.",
                    "absent_or_unproven_events": [],
                    "honest_adaptation": "Use observed movement.",
                }
            if "EVIDENCE-FIRST STORY CONTRACT" in prompt:
                return {
                    "narrative_mode": "mood_montage",
                    "premise": "Distinct movement builds collective focus.",
                    "subject": "The moving group",
                    "observed_goal": "Create a visual movement study",
                    "has_causal_arc": False,
                    "causal_chain": [
                        {
                            "candidate_id": "C0001",
                            "observed_fact": "The first movement begins.",
                            "state_before": "The group is still.",
                            "state_after": "Movement begins.",
                            "story_consequence": "The visual pattern starts.",
                            "evidence_type": "visual",
                        }
                    ],
                    "final_observed_state": "The movement study resolves.",
                    "unsupported_promises": [],
                    "dialogue_policy": "mute_production_chatter",
                    "success_criteria": ["Clear subject", "Visible progression", "Visual payoff"],
                    "recommended_duration_sec": 20,
                }
            if "BLIND VIEWER TEST" in prompt:
                passed = "source-6.mp4" in prompt
                return {
                    "literal_synopsis": "Distinct actions form a movement study." if passed else "Several unrelated setup shots.",
                    "subject": "The group",
                    "apparent_goal": "Create collective movement" if passed else "Unclear",
                    "progression": ["Movement begins", "Energy increases", "The group resolves"] if passed else ["People wait"],
                    "ending": "A visible group payoff." if passed else "The setup stops.",
                    "takeaway_guess": "Small actions create focus." if passed else "Unclear",
                    "coherence_score": 8 if passed else 4,
                    "causal_clarity_score": 7 if passed else 3,
                    "visual_payoff_score": 8 if passed else 4,
                    "confusing_transitions": [] if passed else ["Setup lines do not connect."],
                    "unsupported_or_unresolved_points": [] if passed else ["No result is shown."],
                    "passes": passed,
                    "reason": "The visual progression is legible." if passed else "The sequence lacks progression.",
                }
            if "PICTURE QUALITY RECUT" in prompt:
                return plan(good_sequence, True)
            if "SUPERVISING EDITOR REVIEW" in prompt:
                return plan(bad_sequence, True)
            if "PICTURE ASSEMBLY STEP" in prompt:
                return plan(bad_sequence, False)
            raise AssertionError(prompt[:120])

        director._request_json = fake_request
        result = director.request_sequence(candidates, assets, treatment)

        self.assertIn("picture_recut", calls)
        self.assertTrue(result["candidate_directing"]["quality_gate_passed"])
        self.assertEqual(result["candidate_directing"]["quality_revision_count"], 1)
        self.assertEqual(
            [item["candidate_id"] for item in result["sequence"]],
            ["C0001", "C0007", "C0008", "C0006"],
        )

    def test_nested_coverage_audit_is_normalized_without_losing_absent_events(self):
        director = self.make_director()

        result = director._normalize_coverage_synopsis(
            {
                "project_memory": {
                    "location": "night parking lot",
                    "characters": ["riders", "crew"],
                    "narrative_arc_observed": [
                        "The crew prepares a motorcycle portrait.",
                        "Riders lean forward and hold position.",
                    ],
                    "visual_motifs": None,
                    "unresolved_intentions": ["No departure is visible."],
                },
                "audit_report": {
                    "absent_or_unproven_events": ["The motorcycles ride away."],
                    "source_evidence": "The last visible action is a held pose.",
                    "honest_adaptation": "End on anticipation, not departure.",
                },
                "revised_treatment": {"central_theme": "Preparation as ritual"},
            },
            {"central_theme": "A night ride"},
        )

        self.assertEqual(result["discovered_central_theme"], "Preparation as ritual")
        self.assertEqual(
            result["absent_or_unproven_events"],
            ["The motorcycles ride away."],
        )
        self.assertEqual(result["visual_motifs"], [])
        self.assertIn("held pose", result["observed_ending"])

    def test_no_progress_quality_recut_refuses_known_bad_final(self):
        director = self.make_director()
        director._active_target_duration_sec = 20.0
        treatment = {
            "central_theme": "Observed preparation",
            "edit_style": "hybrid_cinematic",
            "target_duration_sec": 20.0,
        }
        candidates = []
        assets = []
        sequence = []
        for index in range(4):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "asset_id": f"asset-{index}",
                    "source_order": index,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 4.0,
                    "story_role": "context",
                    "visual_summary": f"Observed preparation {index}",
                    "subject_action": f"Action {index}",
                    "camera_motion": "handheld",
                    "shot_scale": "medium",
                    "has_dialogue": False,
                }
            )
            assets.append(
                {
                    "asset_id": f"asset-{index}",
                    "source_video": f"source-{index}.mp4",
                    "duration_sec": 4.0,
                    "transcript": [],
                    "keyframes": [],
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0.0,
                    "trim_out_sec": 3.0,
                    "narrative_function": "context",
                    "viewer_information": f"Information {index}",
                    "reason_for_position": f"Position {index}",
                    "evidence_claim": f"Visible action {index}",
                    "connection_to_previous": "Opens" if index == 0 else "Continues",
                    "audio_intent": "mute_for_music",
                    "music_edit_role": "build",
                }
            )

        def plan(include_review):
            result = {
                "project_summary": "Preparation montage",
                "viewer_takeaway": "The group prepares.",
                "editorial_style": "hybrid_cinematic",
                "graphics_plan": {"strategy": "None", "items": []},
                "sequence": sequence,
            }
            if include_review:
                result["review"] = {
                    "clarity_score": 6,
                    "pacing_score": 6,
                    "visual_storytelling_score": 6,
                    "rhythm_score": 6,
                    "problems_found": ["Repetitive structure"],
                    "changes_made": ["No safe alternative found"],
                    "dialogue_strategy": "No dialogue",
                    "rhythm_strategy": "Build throughout",
                }
            return result

        calls = []

        def fake_request(
            prompt, schema, images=(), model=None, progress_activity="director_generation"
        ):
            calls.append(progress_activity)
            if "CONTINUOUS FULL-FOOTAGE SYNTHESIS" in prompt:
                return {
                    "whole_footage_summary": "Preparation is observed.",
                    "discovered_central_theme": "Preparation",
                    "character_threads": [],
                    "event_timeline": [],
                    "visual_motifs": [],
                    "continuity_risks": [],
                    "observed_ending": "The group remains ready.",
                    "absent_or_unproven_events": [],
                    "honest_adaptation": "Use preparation only.",
                }
            if "EVIDENCE-FIRST STORY CONTRACT" in prompt:
                return {
                    "narrative_mode": "mood_montage",
                    "premise": "Preparation as a visual study.",
                    "subject": "The group",
                    "observed_goal": "Prepare together",
                    "has_causal_arc": False,
                    "causal_chain": [
                        {
                            "candidate_id": "C0001",
                            "observed_fact": "The group prepares.",
                            "state_before": "The group is waiting.",
                            "state_after": "Preparation begins.",
                            "story_consequence": "The visual study starts.",
                            "evidence_type": "visual",
                        }
                    ],
                    "final_observed_state": "The group remains prepared.",
                    "unsupported_promises": [],
                    "dialogue_policy": "natural_texture_only",
                    "success_criteria": ["Clear subject", "Visible pattern", "Deliberate ending"],
                    "recommended_duration_sec": 20,
                }
            if "BLIND VIEWER TEST" in prompt:
                return {
                    "literal_synopsis": "Repeated preparation shots do not resolve.",
                    "subject": "The group",
                    "apparent_goal": "Unclear",
                    "progression": ["Preparation repeats"],
                    "ending": "The group remains waiting.",
                    "takeaway_guess": "Unclear",
                    "coherence_score": 4,
                    "causal_clarity_score": 3,
                    "visual_payoff_score": 4,
                    "confusing_transitions": ["Repeated setup"],
                    "unsupported_or_unresolved_points": ["No result"],
                    "passes": False,
                    "reason": "No readable progression or payoff.",
                }
            if "PICTURE ASSEMBLY STEP" in prompt:
                return plan(False)
            if "SUPERVISING EDITOR REVIEW" in prompt or "PICTURE QUALITY RECUT" in prompt:
                return plan(True)
            raise AssertionError(prompt[:120])

        director._request_json = fake_request
        with self.assertRaises(EditorialQualityError) as raised:
            director.request_sequence(candidates, assets, treatment)

        # One incremental repair is followed by one genuinely different
        # structural reset. Repeating the same edit indefinitely is banned, but
        # a knowingly incoherent plan must never be rendered as a final film.
        self.assertEqual(calls.count("picture_recut"), 2)
        self.assertTrue(raised.exception.violations)

    def test_canonical_coverage_promotes_explicit_missing_event_risk(self):
        director = self.make_director()
        result = director._normalize_coverage_synopsis(
            {
                "whole_footage_summary": "Riders prepare in a parking garage.",
                "discovered_central_theme": "Preparation",
                "character_threads": [],
                "event_timeline": [],
                "visual_motifs": [],
                "continuity_risks": [
                    "The source footage does not show an actual departure."
                ],
                "observed_ending": "Riders hold position.",
                "absent_or_unproven_events": [],
                "honest_adaptation": "End on anticipation.",
            },
            {"central_theme": "Night ride"},
        )

        self.assertEqual(
            result["absent_or_unproven_events"],
            ["The source footage does not show an actual departure."],
        )

    def test_existing_raw_transcript_filters_long_sparse_hallucination(self):
        director = self.make_director()
        result = director._sanitize_transcript_segments(
            [
                {
                    "start_sec": 59.68,
                    "end_sec": 75.92,
                    "text": "难道是",
                },
                {
                    "start_sec": 76.0,
                    "end_sec": 79.0,
                    "text": "我们现在一起戴上头盔",
                },
            ],
            "C1918.MP4",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "我们现在一起戴上头盔")

    def test_picture_plan_metrics_expose_dialogue_graphics_and_repetition(self):
        director = self.make_director()
        candidates = []
        sequence = []
        for index in range(10):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "file_name": f"source-{index // 2}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 7.0,
                    "has_dialogue": index < 8,
                    "camera_motion": "static" if index < 7 else "pan",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0.0,
                    "trim_out_sec": 7.0,
                    "audio_intent": "preserve_dialogue" if index < 8 else "mute_for_music",
                    "narrative_function": "process" if index < 6 else "payoff",
                    "music_edit_role": "release" if index < 9 else "payoff_hit",
                }
            )

        metrics = director._picture_plan_metrics(
            {
                "graphics_plan": {
                    "items": [{"text": str(index)} for index in range(5)]
                },
                "sequence": sequence,
            },
            candidates,
        )

        self.assertEqual(metrics["clip_count"], 10)
        self.assertEqual(metrics["graphic_count"], 5)
        self.assertEqual(metrics["preserved_dialogue_ratio"], 0.8)
        self.assertEqual(metrics["long_dialogue_shots_over_6_sec"], 8)
        self.assertEqual(metrics["longest_same_music_role_run"], 9)

    def test_quality_gate_rejects_static_dialogue_heavy_context_chain(self):
        director = self.make_director()
        candidates = []
        sequence = []
        functions = ["hook", "context", "context", "context", "context", "closure"]
        roles = ["payoff_hit", "build", "build", "build", "build", "payoff_hit"]
        for index in range(6):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 6.0,
                    "has_dialogue": index != 4,
                    "camera_motion": "static" if index != 1 else "handheld",
                    "shot_scale": "wide" if index != 1 else "medium",
                    "story_role": "context",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0.0,
                    "trim_out_sec": 6.0,
                    "audio_intent": "preserve_dialogue" if index != 4 else "natural_texture",
                    "narrative_function": functions[index],
                    "music_edit_role": roles[index],
                }
            )
        candidates.extend(
            [
                {
                    "candidate_id": "ALT1", "camera_motion": "tracking",
                    "shot_scale": "closeup", "story_role": "broll",
                },
                {
                    "candidate_id": "ALT2", "camera_motion": "pan",
                    "shot_scale": "detail", "story_role": "broll",
                },
            ]
        )
        payload = {
            "editorial_style": "kinetic_montage",
            "graphics_plan": {"items": []},
            "sequence": sequence,
        }
        metrics = director._picture_plan_metrics(payload, candidates)
        violations = director._picture_plan_quality_violations(
            payload,
            metrics,
            candidates,
            {"edit_style": "kinetic_montage", "central_theme": "Preparation"},
        )

        combined = " ".join(violations)
        self.assertIn("preserved-dialogue ratio", combined)
        self.assertIn("Static-shot ratio", combined)
        self.assertIn("same narrative function", combined)
        self.assertIn("one music-edit role", combined)
        self.assertIn("no authored escalation", combined)
        self.assertIn("Both the opening teaser and ending", combined)

    def test_quality_gate_allows_explicit_interview_structure(self):
        director = self.make_director(creative_brief="以人物采访为主")
        candidates = []
        sequence = []
        for index, (function, music_role) in enumerate(
            zip(
                ["hook", "escalation", "payoff", "closure"],
                ["natural_sound", "phrase_start", "payoff_hit", "release"],
            )
        ):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "file_name": f"interview-{index}.mp4",
                    "cut_in_sec": 0,
                    "cut_out_sec": 4,
                    "has_dialogue": True,
                    "camera_motion": "static",
                    "shot_scale": "medium",
                    "story_role": "interview",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0,
                    "trim_out_sec": 4,
                    "audio_intent": "preserve_dialogue",
                    "narrative_function": function,
                    "music_edit_role": music_role,
                }
            )
        payload = {
            "editorial_style": "dialogue-led interview",
            "graphics_plan": {"items": []},
            "sequence": sequence,
        }
        metrics = director._picture_plan_metrics(payload, candidates)
        violations = director._picture_plan_quality_violations(
            payload, metrics, candidates, {"edit_style": "dialogue_led"}
        )

        self.assertFalse(any("dialogue" in item.casefold() for item in violations))

    def test_production_chatter_is_advisory_when_director_preserves_it(self):
        director = self.make_director()
        candidates = []
        sequence = []
        functions = ["hook", "context", "contrast", "payoff", "closure"]
        for index in range(5):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0,
                    "cut_out_sec": 8,
                    "has_dialogue": True,
                    "dialogue_ranges_sec": [
                        {"start_sec": 0, "end_sec": 7.5, "text": "假装我们闲聊再拍一条"}
                    ],
                    "production_context_hint": True,
                    "camera_motion": "handheld",
                    "shot_scale": "medium",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0,
                    "trim_out_sec": 8,
                    "audio_intent": "preserve_dialogue",
                    "narrative_function": functions[index],
                    "music_edit_role": "payoff_hit" if index == 3 else "build",
                }
            )
        payload = {
            "editorial_style": "hybrid_cinematic",
            "graphics_plan": {"items": []},
            "sequence": sequence,
        }
        metrics = director._picture_plan_metrics(payload, candidates)
        violations = director._picture_plan_quality_violations(
            payload,
            metrics,
            candidates,
            {"edit_style": "hybrid_cinematic"},
            {
                "narrative_mode": "mood_montage",
                "has_causal_arc": False,
                "causal_chain": [],
            },
        )

        combined = " ".join(violations)
        self.assertNotIn("audible-speech ratio", combined)
        self.assertNotIn("Production-process chatter", combined)

    def test_narrative_contract_downgrades_unproven_bts_arc(self):
        director = self.make_director()
        payload = {
            "narrative_mode": "bts_process",
            "premise": "The crew solves a problem.",
            "subject": "The crew",
            "observed_goal": "Finish the setup",
            "has_causal_arc": True,
            "causal_chain": [
                {
                    "candidate_id": candidate_id,
                    "observed_fact": f"Observed action {candidate_id}",
                    "state_before": "before",
                    "state_after": "after",
                    "story_consequence": "change",
                    "evidence_type": "visual",
                }
                for candidate_id in ("C0001", "C0002", "C0003")
            ],
            "final_observed_state": "The group holds a pose.",
            "unsupported_promises": [],
            "dialogue_policy": "story_dialogue_only",
            "success_criteria": ["a", "b", "c"],
            "recommended_duration_sec": 30,
        }
        normalized = director._normalize_narrative_contract(
            payload,
            [{"candidate_id": f"C000{index}"} for index in range(1, 4)],
            {"event_timeline": []},
        )

        self.assertFalse(normalized["has_causal_arc"])
        self.assertEqual(normalized["narrative_mode"], "mood_montage")
        self.assertIn("contract_correction", normalized)

    def test_natural_texture_speech_is_measured_as_audible(self):
        director = self.make_director()
        candidates = [
            {
                "candidate_id": "C0001",
                "file_name": "source.mp4",
                "cut_in_sec": 0,
                "cut_out_sec": 5,
                "has_dialogue": True,
                "dialogue_ranges_sec": [
                    {"start_sec": 1, "end_sec": 4, "text": "production talk"}
                ],
                "camera_motion": "static",
                "shot_scale": "wide",
            }
        ]
        metrics = director._picture_plan_metrics(
            {
                "graphics_plan": {"items": []},
                "sequence": [
                    {
                        "candidate_id": "C0001",
                        "trim_in_sec": 0,
                        "trim_out_sec": 5,
                        "audio_intent": "natural_texture",
                        "narrative_function": "context",
                        "music_edit_role": "natural_sound",
                    }
                ],
            },
            candidates,
        )

        self.assertEqual(metrics["audible_speech_duration_sec"], 3.0)
        self.assertEqual(metrics["audible_speech_ratio"], 0.6)

    def test_silent_trim_inside_dialogue_candidate_is_not_counted_as_speech(self):
        director = self.make_director()
        candidates = [
            {
                "candidate_id": "C0001",
                "file_name": "source.mp4",
                "cut_in_sec": 0,
                "cut_out_sec": 10,
                "has_dialogue": True,
                "dialogue_ranges_sec": [
                    {"start_sec": 0, "end_sec": 4, "text": "crew talk"}
                ],
                "camera_motion": "static",
                "shot_scale": "wide",
            }
        ]

        metrics = director._picture_plan_metrics(
            {
                "graphics_plan": {"items": []},
                "sequence": [
                    {
                        "candidate_id": "C0001",
                        "trim_in_sec": 6,
                        "trim_out_sec": 9,
                        "audio_intent": "natural_texture",
                        "narrative_function": "context",
                        "music_edit_role": "natural_sound",
                    }
                ],
            },
            candidates,
        )

        self.assertEqual(metrics["audible_speech_duration_sec"], 0.0)
        self.assertEqual(metrics["shot_audit"][0]["source_speech_sec"], 0.0)

    def test_hybrid_dialogue_story_is_not_forced_under_visual_montage_cap(self):
        director = self.make_director()
        candidates = []
        sequence = []
        for index, function in enumerate(
            ["hook", "context", "escalation", "payoff", "closure"]
        ):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0,
                    "cut_out_sec": 6,
                    "has_dialogue": True,
                    "camera_motion": "handheld",
                    "shot_scale": "medium",
                    "story_role": "context",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0,
                    "trim_out_sec": 6,
                    "audio_intent": "preserve_dialogue",
                    "narrative_function": function,
                    "music_edit_role": "payoff_hit" if index in {0, 3} else "build",
                }
            )
        payload = {
            "editorial_style": "hybrid_cinematic",
            "graphics_plan": {"items": []},
            "sequence": sequence,
        }
        metrics = director._picture_plan_metrics(payload, candidates)
        violations = director._picture_plan_quality_violations(
            payload,
            metrics,
            candidates,
            {
                "edit_style": "hybrid_cinematic",
                "development_beat": "The crew discussion reveals the production conflict.",
                "chronology_policy": "teaser_then_chronological",
                "opening_beat": "A teaser of the final pose.",
            },
        )

        combined = " ".join(violations).casefold()
        self.assertNotIn("preserved-dialogue ratio", combined)
        self.assertNotIn("both the opening teaser", combined)

    def test_validator_preserves_director_picture_lock_without_python_recut(self):
        director = self.make_director()
        candidates = []
        sequence = []
        for index in range(8):
            candidate_id = f"C{index + 1:04d}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "asset_id": "asset-1",
                    "source_order": index,
                    "file_name": f"source-{index}.mp4",
                    "cut_in_sec": 0.0,
                    "cut_out_sec": 5.0,
                    "story_role": "context",
                    "visual_summary": f"Observed action {index}",
                }
            )
            sequence.append(
                {
                    "candidate_id": candidate_id,
                    "trim_in_sec": 0.0,
                    "trim_out_sec": 5.0,
                    "narrative_function": "context",
                    "viewer_information": f"Information {index}",
                    "reason_for_position": f"Director position {index}",
                    "evidence_claim": f"Observed action {index}",
                    "connection_to_previous": "Opens." if index == 0 else f"Builds from {index - 1}.",
                    "audio_intent": "natural_texture",
                    "music_edit_role": "build",
                }
            )

        clips = director.validate_sequence(
            {"sequence": sequence},
            candidates,
            {"target_duration_sec": 10.0, "creative_look": "clean_neutral"},
        )

        self.assertEqual([item["candidate_id"] for item in clips], [
            f"C{index + 1:04d}" for index in range(8)
        ])
        self.assertTrue(all(item.get("reason_for_position") for item in clips))

    def test_prompt_token_estimator_protects_chinese_and_ascii(self):
        self.assertGreaterEqual(AIDirector._estimate_prompt_tokens("剪辑" * 100), 200)
        self.assertGreaterEqual(AIDirector._estimate_prompt_tokens("x" * 1000), 400)

    def test_director_checkpoint_round_trip_and_fingerprint_guard(self):
        director = self.make_director()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "raw_data.json"
            checkpoint_path = root / "timeline.director-checkpoint.json"
            raw_path.write_text('{"assets": []}', encoding="utf-8")
            fingerprint = director._checkpoint_fingerprint(raw_path)
            completed = {
                "asset-1|0.000000|10.000000|source.mp4": [
                    {
                        "file_name": "source.mp4",
                        "cut_in_sec": 1.0,
                        "cut_out_sec": 2.0,
                    }
                ]
            }

            director._write_director_checkpoint(
                checkpoint_path, fingerprint, completed
            )

            self.assertEqual(
                director._load_director_checkpoint(
                    checkpoint_path, fingerprint
                ),
                completed,
            )
            self.assertEqual(
                director._load_director_checkpoint(
                    checkpoint_path, "different-input"
                ),
                {},
            )

    def test_chunk_minutes_constraint(self):
        with self.assertRaises(DirectorError):
            self.make_director(chunk_minutes=9)

    def test_72b_text_context_is_capped_without_changing_vision_context(self):
        director = self.make_director(
            model="qwen3.5:35b-a3b",
            text_model="qwen2.5:72b-instruct-q5_K_M",
            num_ctx=16384,
        )

        self.assertEqual(director._effective_num_ctx(director.model), 16384)
        self.assertEqual(director._effective_num_ctx(director.text_model), 8192)

    def test_end_to_end_with_fake_ollama_unloads(self):
        raw = self.raw_data(duration=100.0)
        raw["transcript"] = [
            {"start_sec": 1.0, "end_sec": 30.0, "text": "test"}
        ]
        session = FakeSession()
        director = self.make_director(session=session)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "raw_data.json"
            output_path = root / "timeline_cuts.json"
            raw_path.write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )
            output = director.run(raw_path, output_path)
            self.assertTrue(output_path.is_file())
            self.assertEqual(output["clips"][0]["clip_id"], 1)
            self.assertEqual(output["clips"][0]["file_name"], "proxy.mp4")
            self.assertTrue(
                any(item.get("keep_alive") == 0 for item in session.posts)
            )

    def test_multi_asset_run_sends_images_then_globally_sequences(self):
        session = VisionSession()
        director = self.make_director(
            model="qwen3.5:test", session=session
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = root / "frame.jpg"
            frame.write_bytes(b"fake-jpeg")
            source = root / "source.mp4"
            source.write_bytes(b"fake-media")
            raw = {
                "schema_version": "2.0",
                "assets": [
                    {
                        "asset_id": "asset-1",
                        "duration_sec": 10.0,
                        "source_video": str(source),
                        "proxy_file_name": str(source),
                        "transcript": [
                            {
                                "start_sec": 0.0,
                                "end_sec": 5.0,
                                "text": "opening narration",
                            }
                        ],
                        "keyframes": [
                            {
                                "timestamp_sec": 2.0,
                                "file_name": "frame.jpg",
                                "image_path": str(frame),
                                "scene_score": 0.8,
                            }
                        ],
                    }
                ],
            }
            raw_path = root / "raw_data.json"
            output_path = root / "timeline_cuts.json"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            output = director.run(raw_path, output_path)

            image_requests = [item for item in session.posts if item.get("images")]
            # Treatment is now downstream of the neutral full review, so only
            # the temporal evidence request sends images.
            self.assertEqual(len(image_requests), 1)
            self.assertEqual(output["schema_version"], "3.0")
            self.assertEqual(
                output["visual_review"]["mode"], "neutral_complete_temporal_coverage"
            )
            self.assertTrue(
                output["visual_review"]["second_stage_frame_subsampling"]
            )
            self.assertEqual(
                output["visual_review"]["temporal_refinement_mode"],
                "dense_refinement_not_completed_original_atoms_preserved",
            )
            self.assertIn("discovered_central_theme", output["full_review_synopsis"])
            self.assertEqual(
                output["color_pipeline"]["sources"]["asset-1"]["resolve_input_gamma"],
                "S-Log3",
            )
            self.assertEqual(
                output["director_treatment"]["chronology_policy"],
                "strict_chronological",
            )
            self.assertEqual(output["clips"][0]["file_name"], str(source))
            self.assertEqual(output["clips"][0]["transition_to_next"], "cut")
            self.assertEqual(
                output["clips"][0]["requested_transition_to_next"],
                "cross_dissolve",
            )
            self.assertEqual(output["clips"][0]["audio_cleanup"], "strong")
            self.assertTrue(output["clips"][0]["has_dialogue"])
            self.assertTrue(output_path.is_file())

    def test_oversized_prompt_is_blocked_before_ollama_can_left_truncate(self):
        session = FakeSession()
        director = self.make_director(session=session, num_ctx=4096)

        with self.assertRaisesRegex(DirectorError, "Request blocked"):
            director._request_json("x" * 50000, {"type": "object"})

        self.assertEqual(session.posts, [])

    def test_paged_director_reviews_every_candidate_without_fixed_top_n(self):
        director = self.make_director(num_ctx=4096)
        candidates = [
            {
                "candidate_id": f"C{index:04d}",
                "asset_id": "asset-1",
                "source_order": 0,
                "source": "source.mp4",
                "in": float(index),
                "out": float(index + 2),
                "story_role": "context",
                "visual_summary": ("distinct chronological visual evidence " * 4).strip(),
                "dialogue_excerpt": "",
                "quality_score": 0.8,
            }
            for index in range(1, 31)
        ]

        def fake_request(
            prompt,
            schema,
            images=(),
            model=None,
            progress_activity="director_generation",
        ):
            page = json.loads(prompt.split("CANDIDATE PAGE:\n", 1)[1])
            keep = schema["properties"]["recommendations"]["maxItems"]
            return {
                "page_summary": "All candidates on this page were compared.",
                "recommendations": [
                    {
                        "candidate_id": item["candidate_id"],
                        "story_value": "Unique evidence for the chronological story.",
                        "suggested_story_role": "context",
                    }
                    for item in page[:keep]
                ],
            }

        director._request_json = fake_request
        selected, audit = director._review_candidate_round(
            candidates,
            {"central_theme": "A complete chronology"},
            [{"asset_id": "asset-1", "source_order": 0}],
            {"whole_footage_summary": "The source develops from setup to payoff."},
            round_number=1,
        )

        reviewed_ids = {
            candidate_id
            for page in audit["pages"]
            for candidate_id in page["input_candidate_ids"]
        }
        self.assertEqual(reviewed_ids, {item["candidate_id"] for item in candidates})
        self.assertEqual(audit["input_candidate_count"], len(candidates))
        self.assertGreater(len(selected), 0)
        self.assertLess(len(selected), len(candidates))

    def test_sixty_shot_picture_lock_uses_bounded_pages_without_losing_a_shot(self):
        director = self.make_director(num_ctx=8192)
        candidates = [
            {
                "candidate_id": f"C{index:04d}",
                "in": float(index * 5),
                "out": float(index * 5 + 4),
                "visual_summary": f"Observed action {index}",
                "story_role": "context",
            }
            for index in range(1, 61)
        ]
        ordered_ids = [item["candidate_id"] for item in candidates]
        requests = []

        def required_ids(prompt):
            return json.loads(
                prompt.split("REQUIRED IDS:\n", 1)[1].split("\n", 1)[0]
            )

        def page_number(prompt, marker):
            return int(prompt.split(marker, 1)[1].split(".", 1)[0])

        def fake_request(
            prompt, schema, images=(), model=None,
            progress_activity="director_generation",
        ):
            requests.append((prompt, schema))
            if "PICTURE ORDER MANIFEST" in prompt:
                return {
                    "project_summary": "A complete sixty-shot film.",
                    "viewer_takeaway": "A long observed process develops clearly.",
                    "editorial_style": "narrative_documentary",
                    "ordered_candidate_ids": ordered_ids,
                }
            if "PICTURE SKELETON PAGE" in prompt:
                ids = required_ids(prompt)
                page = page_number(prompt, "PICTURE SKELETON PAGE ")
                return {
                    "page_index": page,
                    "shots": [
                        {
                            "candidate_id": candidate_id,
                            "trim_in_sec": next(
                                item["in"] for item in candidates
                                if item["candidate_id"] == candidate_id
                            ),
                            "trim_out_sec": next(
                                item["out"] for item in candidates
                                if item["candidate_id"] == candidate_id
                            ),
                            "narrative_function": (
                                "hook" if candidate_id == ordered_ids[0]
                                else "closure" if candidate_id == ordered_ids[-1]
                                else "context"
                            ),
                            "audio_intent": "natural_texture",
                            "music_edit_role": "build",
                        }
                        for candidate_id in ids
                    ],
                }
            if "PICTURE ENRICHMENT PAGE" in prompt:
                ids = required_ids(prompt)
                page = page_number(prompt, "PICTURE ENRICHMENT PAGE ")
                return {
                    "page_index": page,
                    "shots": [
                        {
                            "candidate_id": candidate_id,
                            "viewer_information": f"Information from {candidate_id}",
                            "reason_for_position": f"Progression at {candidate_id}",
                            "evidence_claim": f"Observed action in {candidate_id}",
                            "connection_to_previous": "Visible chronological progression.",
                            "transition_to_next": "cut",
                            "transition_duration_sec": 0.0,
                            "audio_cleanup": "none",
                            "color_look": "neutral",
                            "motion": "static",
                            "volume_db": 0.0,
                            "drx_preset": "none",
                            "stabilization": "none",
                            "tracking": "none",
                            "smart_reframe": False,
                        }
                        for candidate_id in ids
                    ],
                }
            if "PICTURE GRAPHICS PASS" in prompt:
                return {"graphics_plan": {"strategy": "No graphics.", "items": []}}
            raise AssertionError(prompt[:120])

        director._request_json = fake_request
        result = director._request_staged_picture_plan(
            "Author one coherent observed film.",
            candidates,
            include_review=False,
            progress_activity="picture_assembly",
        )

        self.assertEqual(
            [item["candidate_id"] for item in result["sequence"]], ordered_ids
        )
        self.assertTrue(all(item.get("evidence_claim") for item in result["sequence"]))
        audit = result["_staged_output_audit"]
        self.assertEqual(audit["selected_shot_count"], 60)
        self.assertEqual(audit["full_verbose_sequence_requests"], 0)
        self.assertLessEqual(audit["max_verbose_shots_in_any_request"], 6)
        self.assertEqual(audit["skeleton_pages"], 5)
        self.assertEqual(audit["enrichment_pages"], 10)
        # No output schema can ask for one complete sixty-shot verbose sequence.
        # The only 60-item response is the compact ID manifest.
        for _prompt, schema in requests:
            properties = schema.get("properties", {})
            self.assertNotIn("sequence", properties)
            shots = properties.get("shots", {})
            if shots:
                self.assertLessEqual(shots["maxItems"], 12)

    def test_dense_temporal_sampling_adapts_to_32k_and_8k_vision_context(self):
        item = {
            "candidate_id": "C0001",
            "asset_id": "asset-1",
            "cut_in_sec": 0.0,
            "cut_out_sec": 8.0,
            "visual_summary": "A continuous observed action develops.",
            "subject_action": "The subject completes one gesture.",
        }
        director_32k = self.make_director(num_ctx=32768)
        fps_32k, frames_32k = director_32k._dense_refinement_sampling_plan(
            item, 0.0, 8.0
        )
        director_8k = self.make_director(num_ctx=8192)
        fps_8k, frames_8k = director_8k._dense_refinement_sampling_plan(
            item, 0.0, 8.0
        )

        self.assertGreaterEqual(frames_32k, 2)
        self.assertLess(frames_32k, 28)
        self.assertGreaterEqual(frames_8k, 2)
        self.assertLess(frames_8k, frames_32k)
        self.assertLessEqual(fps_8k, fps_32k)
        self.assertLessEqual(fps_32k, 4.0)
        oversized_times = [round(index * 8.0 / 27.0, 4) for index in range(28)]
        oversized_prompt = director_32k._dense_refinement_prompt(
            item, 0.0, 8.0, oversized_times
        )
        self.assertFalse(
            director_32k._request_has_multimodal_capacity(
                oversized_prompt,
                ATOM_REFINEMENT_SCHEMA,
                28,
            )
        )

    def test_music_quality_gate_fails_closed_after_two_measured_mismatches(self):
        director = self.make_director()
        with tempfile.TemporaryDirectory() as temporary:
            track = Path(temporary) / "flat.wav"
            track.touch()
            director._music_files = [track]
            director._music_analysis = {
                "tracks": [
                    {
                        "file_name": str(track),
                        "duration_sec": 60.0,
                        "energy_profile": {
                            "trend": "flat", "build_score": 0.05,
                            "contrast_db": 0.5,
                        },
                        "sections": [
                            {"start_sec": 0.0, "end_sec": 30.0, "energy": "low"}
                        ],
                    }
                ]
            }
            calls = []

            def invalid_music(*args, **kwargs):
                calls.append(1)
                return {
                    "music_plan": {
                        "strategy": "A rising climax.",
                        "silence_regions": [],
                        "cues": [
                            {
                                "cue_id": "M1", "track_file": "flat.wav",
                                "story_beat": "payoff", "timeline_in_sec": 0,
                                "timeline_out_sec": 10, "track_in_sec": 0,
                                "track_out_sec": 10, "reason": "A rising climax.",
                                "target_lufs": -18, "fade_in_sec": 1,
                                "fade_out_sec": 1, "crossfade_sec": 0,
                                "duck_under_dialogue_db": -10, "sync_points": [],
                            }
                        ],
                    }
                }

            director._request_json = invalid_music
            with self.assertRaisesRegex(DirectorError, "quality-first"):
                director._request_quality_gated_music_plan(
                    "Choose measured music.",
                    10.0,
                    {"music_energy_arc": "rising into a climax"},
                )
            self.assertEqual(len(calls), 2)

            director._request_json = lambda *args, **kwargs: {
                "music_plan": {
                    "strategy": "Intentional silence.",
                    "silence_regions": [],
                    "cues": [],
                }
            }
            silent = director._request_quality_gated_music_plan(
                "Music is optional.", 10.0,
                {"music_energy_arc": "rising into a climax"},
            )
            self.assertEqual(silent["cues"], [])
            self.assertEqual(silent["mode"], "none")

    def test_real_video_duration_clamps_transcript_and_candidate_tail(self):
        director = self.make_director()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "raw_data.json"
            raw_path.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "asset_id": "a1",
                                "duration_sec": 14.0,
                                "video": {"duration_sec": 12.5125},
                                "source_video": "source.mp4",
                                "transcript": [
                                    {"start_sec": 10.0, "end_sec": 14.0, "text": "tail"}
                                ],
                                "keyframes": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = director.load_raw_data(raw_path)
            asset = payload["assets"][0]
            candidates = director._sanitize_candidate_bounds(
                [
                    {
                        "asset_id": "a1",
                        "file_name": "source.mp4",
                        "cut_in_sec": 5.0,
                        "cut_out_sec": 14.0,
                    }
                ],
                [asset],
            )

            self.assertEqual(asset["duration_sec"], 12.5125)
            self.assertEqual(asset["transcript"][0]["end_sec"], 12.512)
            self.assertEqual(candidates[0]["cut_out_sec"], 12.512)

    def test_dialogue_overlap_forces_music_ducking(self):
        director = self.make_director()
        plan = director.enforce_dialogue_ducking(
            [
                {"cut_in_sec": 0, "cut_out_sec": 4, "has_dialogue": True},
                {"cut_in_sec": 5, "cut_out_sec": 7, "has_dialogue": False},
            ],
            {
                "cues": [
                    {
                        "timeline_in_sec": 0,
                        "timeline_out_sec": 6,
                        "duck_under_dialogue_db": 0,
                    }
                ]
            },
        )

        self.assertEqual(plan["cues"][0]["duck_under_dialogue_db"], -10.0)

    def test_final_sequence_refines_candidate_and_obeys_director_audio_intent(self):
        director = self.make_director()
        treatment = {
            "chronology_policy": "teaser_then_chronological",
            "target_duration_sec": 45,
            "creative_look": "cool_steel",
            "edit_style": "kinetic_montage",
        }
        candidate = {
            "candidate_id": "C0001", "asset_id": "a1", "source_order": 0,
            "file_name": "source.mp4", "cut_in_sec": 0, "cut_out_sec": 12,
            "story_role": "opening", "reason_for_cut": "Rider adjusts gloves",
            "visual_summary": "Rider prepares beside a motorcycle",
            "subject_action": "Adjusting gloves", "dialogue_excerpt": "麦克风不需要录声音",
            "quality_score": 0.9, "confidence": 0.9,
        }

        clips = director.validate_sequence(
            {
                "sequence": [
                    {
                        "candidate_id": "C0001", "trim_in_sec": 2.0,
                        "trim_out_sec": 5.5, "narrative_function": "hook",
                        "viewer_information": "The riders treat preparation as ritual.",
                        "reason_for_position": "A tactile action creates an immediate promise.",
                        "audio_intent": "preserve_dialogue", "music_edit_role": "phrase_start",
                    }
                ]
            },
            [candidate],
            treatment,
        )

        self.assertEqual((clips[0]["cut_in_sec"], clips[0]["cut_out_sec"]), (2.0, 5.5))
        self.assertEqual(clips[0]["narrative_function"], "hook")
        self.assertEqual(clips[0]["audio_intent"], "preserve_dialogue")
        self.assertEqual(clips[0]["volume_db"], 0.0)
        self.assertNotIn("production_chatter_muted", clips[0])

    def test_graphics_plan_is_grounded_to_surviving_picture_lock(self):
        director = self.make_director()
        clips = [
            {"candidate_id": "C1", "cut_in_sec": 0, "cut_out_sec": 3},
            {"candidate_id": "C2", "cut_in_sec": 5, "cut_out_sec": 9},
        ]
        plan = director.validate_graphics_plan(
            {
                "strategy": "State the promise, then mark the payoff.",
                "items": [
                    {
                        "graphic_id": "G1", "kind": "chapter",
                        "anchor_candidate_id": "C2", "placement": "clip_middle",
                        "duration_sec": 2, "text": "READY", "subtitle": "",
                        "style": "kinetic", "purpose": "Name the payoff",
                    },
                    {
                        "graphic_id": "BAD", "kind": "chapter",
                        "anchor_candidate_id": "REMOVED", "placement": "clip_start",
                        "duration_sec": 2, "text": "INVALID", "subtitle": "",
                        "style": "minimal", "purpose": "Removed anchor",
                    },
                ],
            },
            clips,
            {"title": "Night Shift", "viewer_takeaway": "Preparation becomes unity."},
        )

        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["text"], "READY")
        self.assertEqual(plan["items"][0]["timeline_in_sec"], 4.0)
        self.assertEqual(plan["items"][0]["timeline_out_sec"], 6.0)

    def test_director_authored_silent_opening_is_not_overridden(self):
        director = self.make_director()
        with tempfile.TemporaryDirectory() as temporary:
            track = Path(temporary) / "score.wav"
            track.touch()
            director._music_analysis = {
                "tracks": [
                    {
                        "file_name": str(track), "duration_sec": 180,
                        "integrated_lufs": -14, "beats_sec": [],
                        "strong_beats_sec": [], "downbeats_sec": [], "sections": [],
                    }
                ]
            }
            plan = director.validate_music_plan(
                {
                    "strategy": "one build", "silence_regions": [],
                    "cues": [
                        {
                            "cue_id": "M1", "track_file": "score.wav",
                            "story_beat": "development", "timeline_in_sec": 24,
                            "timeline_out_sec": 70, "track_in_sec": 79,
                            "track_out_sec": 125, "reason": "build",
                            "target_lufs": -18, "fade_in_sec": 1,
                            "fade_out_sec": 1, "crossfade_sec": 0,
                            "duck_under_dialogue_db": -9, "sync_points": [],
                        }
                    ],
                },
                70,
            )

        cue = plan["cues"][0]
        self.assertEqual(cue["timeline_in_sec"], 24.0)
        self.assertEqual(cue["track_in_sec"], 79.0)
        self.assertEqual(cue["timeline_out_sec"], 70.0)


if __name__ == "__main__":
    unittest.main()
