import json
from pathlib import Path
import tempfile
import unittest

from src.music_analyzer import LicensedMusicAnalyzer


class LicensedMusicAnalyzerTests(unittest.TestCase):
    def test_manifest_license_and_keyword_ranking_are_preserved(self):
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
            self.assertEqual(ranked[1]["license"], "user-supplied")


if __name__ == "__main__":
    unittest.main()
