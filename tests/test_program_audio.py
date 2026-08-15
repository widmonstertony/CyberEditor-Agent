"""Standard-library tests for frame-faithful production-audio conforming."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from src.frame_edl import build_frame_edl
from src.program_audio import ProgramAudioRenderer


def framed(payload, source_rates):
    """Attach a deterministic canonical schedule to one test plan."""
    payload["project_fps"] = 25
    for index, clip in enumerate(payload["clips"], start=1):
        clip.setdefault("clip_id", index)
    payload["frame_edl"] = build_frame_edl(
        payload, source_fps_overrides=source_rates
    )
    return payload


class ProgramAudioRendererTests(unittest.TestCase):
    def test_mute_for_music_uses_silence_even_when_dialogue_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "spoken.mp4"
            source.write_bytes(b"mock")
            timeline = root / "timeline_cuts.json"
            timeline.write_text(
                json.dumps(framed({
                    "clips": [{
                        "file_name": str(source),
                        "cut_in_sec": 2.0,
                        "cut_out_sec": 5.0,
                        "audio_intent": "mute_for_music",
                        "audio_cleanup": "strong",
                        "has_dialogue": True,
                        "volume_db": -60,
                    }]
                }, [25])),
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
            self.assertAlmostEqual(duration, 3.0)
            self.assertIn("anullsrc=r=48000:cl=stereo", command)
            self.assertNotIn("atrim=start=2.000000:end=5.000000", graph)
            self.assertNotIn("loudnorm", graph)
            self.assertNotIn("afftdn", graph)
            self.assertNotIn("volume=-3.000dB", graph)

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
                    framed({
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
                    }, [25, 25])
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
            # 7.5 seconds falls between 25-fps frames.  The canonical schedule
            # keeps the complete source frame, then conforms audio to 63 record
            # frames instead of independently trimming back to 2.5 seconds.
            self.assertAlmostEqual(duration, 3.52)
            self.assertIn("atrim=start=5.000000:end=7.520000", graph)
            self.assertIn("atrim=start=1.000000:end=2.000000", graph)
            self.assertIn("loudnorm=I=-18:TP=-2:LRA=11", graph)
            self.assertIn("volume=-3.000dB", graph)
            self.assertIn("volume=-6.000dB", graph)
            self.assertIn("concat=n=2:v=0:a=1", graph)


if __name__ == "__main__":
    unittest.main()
