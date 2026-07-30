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


if __name__ == "__main__":
    unittest.main()
