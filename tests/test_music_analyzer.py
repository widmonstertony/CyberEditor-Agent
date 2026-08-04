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

        self.assertIn("instrumental no vocals", queries[0])

    def test_search_query_stays_compact_while_preserving_build_intent(self):
        queries = MusicCandidateAcquirer._queries(
            {
                "search_queries": ["night documentary score"],
                "vocal_policy": "instrumental_only",
                "tempo_bpm": {"min": 78, "max": 96},
                "emotion_arc": "quiet build to a late peak",
            },
            "",
        )

        self.assertIn("instrumental no vocals", queries[0])
        self.assertIn("gradual build", queries[0])
        self.assertNotIn("78-96 BPM", queries[0])
        self.assertNotIn("quiet build to a late peak", queries[0])
        self.assertLessEqual(len(queries[0]), 110)

    def test_search_failure_reuses_only_audited_managed_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cached.wav").write_bytes(b"cached")
            (root / "library.json").write_text(
                json.dumps({
                    "managed_provider_cache": True,
                    "tracks": [{
                        "file": "cached.wav",
                        "provider": "yt_dlp_unverified",
                    }],
                }),
                encoding="utf-8",
            )
            (root / "rights_audit.json").write_text(
                json.dumps({"provider": "yt_dlp_unverified", "sources": []}),
                encoding="utf-8",
            )

            acquirer = MusicCandidateAcquirer(root)

            self.assertEqual(acquirer._audited_cache_track_count(), 1)
            self.assertEqual(
                acquirer._reuse_audited_cache("offline"), root.resolve()
            )

    def test_energy_arc_detects_a_late_rise(self):
        profile = MusicCandidateAcquirer._summarize_energy_arc(
            [
                {"time_sec": 0, "dbfs": -28},
                {"time_sec": 10, "dbfs": -27},
                {"time_sec": 20, "dbfs": -23},
                {"time_sec": 30, "dbfs": -20},
                {"time_sec": 40, "dbfs": -15},
                {"time_sec": 50, "dbfs": -12},
            ]
        )

        self.assertEqual(profile["trend"], "rising")
        self.assertGreater(profile["build_score"], 0.55)
        self.assertGreater(profile["peak_time_ratio"], 0.8)

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
