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
        if "Build one coherent documentary edit" in json.get("prompt", ""):
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

    def test_parse_fenced_json(self):
        parsed = AIDirector.parse_generated_json(
            '```json\n{"decisions": []}\n```'
        )
        self.assertEqual(parsed, {"decisions": []})

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
            self.assertEqual(len(image_requests), 1)
            self.assertEqual(output["schema_version"], "2.0")
            self.assertEqual(output["clips"][0]["file_name"], str(source))
            self.assertEqual(
                output["clips"][0]["transition_to_next"], "cross_dissolve"
            )
            self.assertEqual(output["clips"][0]["audio_cleanup"], "strong")
            self.assertTrue(output_path.is_file())


if __name__ == "__main__":
    unittest.main()
