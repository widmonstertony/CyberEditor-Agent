"""Standard-library tests for chunking and Ollama response handling."""

import json
import logging
from pathlib import Path
import tempfile
import unittest

from src.director import AIDirector, DirectorError


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
        if "Create the director treatment" in prompt:
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
        self.assertEqual(len(generation_posts), 2)
        self.assertIn("step 1", generation_posts[0]["prompt"].casefold())
        self.assertIn(
            "Design the final documentary music cue sheet",
            generation_posts[1]["prompt"],
        )
        self.assertNotIn("AVAILABLE MUSIC", generation_posts[0]["prompt"])
        self.assertEqual(result["sequence"][0]["candidate_id"], "C0001")
        self.assertIn("music_plan", result)

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
            # The first multimodal pass sees representative project frames;
            # the second request inspects the source chunk in detail.
            self.assertEqual(len(image_requests), 2)
            self.assertEqual(output["schema_version"], "3.0")
            self.assertEqual(
                output["color_pipeline"]["sources"]["asset-1"]["resolve_input_gamma"],
                "S-Log3",
            )
            self.assertEqual(
                output["director_treatment"]["chronology_policy"],
                "strict_chronological",
            )
            self.assertEqual(output["clips"][0]["file_name"], str(source))
            self.assertEqual(
                output["clips"][0]["transition_to_next"], "cross_dissolve"
            )
            self.assertEqual(output["clips"][0]["audio_cleanup"], "strong")
            self.assertTrue(output["clips"][0]["has_dialogue"])
            self.assertTrue(output_path.is_file())


if __name__ == "__main__":
    unittest.main()
