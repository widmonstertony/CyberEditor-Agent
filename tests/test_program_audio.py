"""Standard-library tests for frame-faithful production-audio conforming."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from src.program_audio import ProgramAudioRenderer


class ProgramAudioRendererTests(unittest.TestCase):
    def test_build_command_uses_exact_picture_ranges_and_dialogue_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_a = root / "a.mp4"
            source_b = root / "b.mp4"
            source_a.write_bytes(b"mock")
            source_b.write_bytes(b"mock")
            timeline = root / "timeline_cuts.json"
            timeline.write_text(
                json.dumps(
                    {
                        "clips": [
                            {
                                "file_name": str(source_a),
                                "cut_in_sec": 5.0,
                                "cut_out_sec": 7.5,
                                "audio_cleanup": "light",
                                "has_dialogue": True,
                                "volume_db": -18,
                            },
                            {
                                "file_name": str(source_b),
                                "cut_in_sec": 1.0,
                                "cut_out_sec": 2.0,
                                "audio_cleanup": "none",
                                "has_dialogue": False,
                                "volume_db": -6,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            renderer = ProgramAudioRenderer(
                timeline,
                root / "program.wav",
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
            )
            payload = renderer.load_timeline()
            with mock.patch.object(renderer, "_has_audio_stream", return_value=True):
                command, duration = renderer.build_command(payload)

            graph = command[command.index("-filter_complex") + 1]
            self.assertAlmostEqual(duration, 3.5)
            self.assertIn("atrim=start=5.000000:end=7.500000", graph)
            self.assertIn("atrim=start=1.000000:end=2.000000", graph)
            self.assertIn("loudnorm=I=-18:TP=-2:LRA=11", graph)
            self.assertIn("volume=-3.000dB", graph)
            self.assertIn("volume=-6.000dB", graph)
            self.assertIn("concat=n=2:v=0:a=1", graph)


if __name__ == "__main__":
    unittest.main()
