"""Unit tests for final Resolve delivery QA."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.final_output_qa import FinalOutputQA, FinalOutputQAError


class FinalOutputQATests(unittest.TestCase):
    def make_qa(self):
        with mock.patch("src.final_output_qa.shutil.which", return_value="tool"):
            return FinalOutputQA()

    @staticmethod
    def probe(path, duration=10.0, audio_duration=10.0):
        return {
            "path": str(path),
            "container_duration_sec": duration,
            "video_duration_sec": duration,
            "audio_duration_sec": audio_duration,
            "fps": 25.0,
            "has_video": True,
            "has_audio": True,
        }

    def test_matching_delivery_passes(self):
        qa = self.make_qa()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.mp4"
            final = root / "final.mov"
            approved.write_bytes(b"a")
            final.write_bytes(b"b")
            with mock.patch.object(qa, "_probe", side_effect=lambda path: self.probe(path)), mock.patch.object(
                qa, "_sampled_ssim", return_value=0.97
            ):
                result = qa.run(final, approved, root / "qa.json")
        self.assertTrue(result["passes"])

    def test_shifted_video_and_short_audio_are_blocked(self):
        qa = self.make_qa()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.mp4"
            final = root / "final.mov"
            approved.write_bytes(b"a")
            final.write_bytes(b"b")

            def fake_probe(path):
                if Path(path).name == "final.mov":
                    return self.probe(path, duration=15.0, audio_duration=10.0)
                return self.probe(path, duration=10.0, audio_duration=10.0)

            with mock.patch.object(qa, "_probe", side_effect=fake_probe), mock.patch.object(
                qa, "_sampled_ssim", return_value=0.40
            ):
                with self.assertRaises(FinalOutputQAError):
                    qa.run(final, approved, root / "qa.json")
                report = (root / "qa.json").read_text(encoding="utf-8")
        self.assertIn("Duration changed", report)
        self.assertIn("picture/audio lengths differ", report)


if __name__ == "__main__":
    unittest.main()

