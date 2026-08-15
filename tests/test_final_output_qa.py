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
            ), mock.patch.object(
                qa, "_scene_change_signature", return_value=[2.0, 6.0]
            ), mock.patch.object(
                qa,
                "_audio_energy_fingerprint",
                return_value={
                    "envelope_db": [-30.0, -20.0, -25.0, -35.0],
                    "overall_dbfs": -24.0,
                    "active_fraction": 1.0,
                },
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
            ), mock.patch.object(
                qa, "_scene_change_signature", return_value=[2.0, 6.0]
            ), mock.patch.object(
                qa,
                "_audio_energy_fingerprint",
                return_value={
                    "envelope_db": [-30.0, -20.0, -25.0, -35.0],
                    "overall_dbfs": -24.0,
                    "active_fraction": 1.0,
                },
            ):
                with self.assertRaises(FinalOutputQAError):
                    qa.run(final, approved, root / "qa.json")
                report = (root / "qa.json").read_text(encoding="utf-8")
        self.assertIn("Duration changed", report)
        self.assertIn("picture/audio lengths differ", report)

    def test_silent_or_unrelated_final_audio_is_blocked(self):
        qa = self.make_qa()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.mp4"
            final = root / "final.mov"
            approved.write_bytes(b"a")
            final.write_bytes(b"b")

            def fake_audio(path):
                if Path(path).name == "final.mov":
                    return {
                        "envelope_db": [-100.0] * 8,
                        "overall_dbfs": -100.0,
                        "active_fraction": 0.0,
                    }
                return {
                    "envelope_db": [-22.0, -18.0, -30.0, -16.0] * 2,
                    "overall_dbfs": -21.0,
                    "active_fraction": 1.0,
                }

            with mock.patch.object(
                qa, "_probe", side_effect=lambda path: self.probe(path)
            ), mock.patch.object(
                qa, "_sampled_ssim", return_value=0.99
            ), mock.patch.object(
                qa, "_scene_change_signature", return_value=[2.0, 6.0]
            ), mock.patch.object(
                qa, "_audio_energy_fingerprint", side_effect=fake_audio
            ):
                with self.assertRaisesRegex(FinalOutputQAError, "silent"):
                    qa.run(final, approved, root / "qa.json")

    def test_wrong_edit_boundary_pattern_is_blocked_even_when_duration_matches(self):
        qa = self.make_qa()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.mp4"
            final = root / "final.mov"
            approved.write_bytes(b"a")
            final.write_bytes(b"b")

            def scenes(path):
                return [2.0, 5.0, 8.0] if Path(path).name == "approved.mp4" else [3.5, 7.0]

            with mock.patch.object(
                qa, "_probe", side_effect=lambda path: self.probe(path)
            ), mock.patch.object(
                qa, "_sampled_ssim", return_value=0.90
            ), mock.patch.object(
                qa, "_scene_change_signature", side_effect=scenes
            ), mock.patch.object(
                qa,
                "_audio_energy_fingerprint",
                return_value={
                    "envelope_db": [-30.0, -20.0, -25.0, -35.0],
                    "overall_dbfs": -24.0,
                    "active_fraction": 1.0,
                },
            ):
                with self.assertRaisesRegex(FinalOutputQAError, "boundaries"):
                    qa.run(final, approved, root / "qa.json")


if __name__ == "__main__":
    unittest.main()
