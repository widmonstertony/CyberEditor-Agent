import json
from pathlib import Path
import tempfile
import unittest

from src.music_analyzer import (
    LicensedMusicAnalyzer,
    MusicAnalysisError,
    MusicCandidateAcquirer,
)


class LicensedMusicAnalyzerTests(unittest.TestCase):
    def test_manifest_is_authoritative_and_stale_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calm.wav").write_bytes(b"not-decoded-in-this-test")
            (root / "epic.wav").write_bytes(b"not-decoded-in-this-test")
            (root / "library.json").write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "file": "epic.wav",
                                "title": "Licensed Epic",
                                "mood": "epic cinematic",
                                "tags": ["climax"],
                                "licensed": True,
                                "license": "CC BY 4.0",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            analyzer = LicensedMusicAnalyzer(root)
            ranked = analyzer.rank(analyzer.discover(), "epic climax")

            self.assertEqual(ranked[0]["title"], "Licensed Epic")
            self.assertEqual(ranked[0]["license"], "CC BY 4.0")
            self.assertEqual(len(ranked), 1)

    def test_spoken_word_manifest_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "interview.wav").write_bytes(b"not-decoded-in-this-test")
            (root / "score.wav").write_bytes(b"not-decoded-in-this-test")
            (root / "library.json").write_text(
                json.dumps(
                    {
                        "managed_provider_cache": True,
                        "tracks": [
                            {"file": "interview.wav", "title": "Actor interview and conversation"},
                            {"file": "score.wav", "title": "Cinematic instrumental score"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            tracks = LicensedMusicAnalyzer(root).discover()

            self.assertEqual([item["title"] for item in tracks], ["Cinematic instrumental score"])

    def test_instrumental_brief_strengthens_search_query(self):
        queries = MusicCandidateAcquirer._queries(
            {"search_queries": ["night ride"], "vocal_policy": "instrumental_only"},
            "",
        )

        self.assertIn("instrumental background music no vocals", queries[0])

    def test_arbitrary_online_audio_fails_closed_without_per_run_consent(self):
        with tempfile.TemporaryDirectory() as temporary:
            acquirer = MusicCandidateAcquirer(temporary)

            with self.assertRaisesRegex(MusicAnalysisError, "确认|confirm"):
                acquirer.acquire_ytdlp(
                    {"search_queries": ["cinematic instrumental"]},
                    "",
                    3,
                    rights_confirmed=False,
                    rights_claim="",
                )


if __name__ == "__main__":
    unittest.main()
