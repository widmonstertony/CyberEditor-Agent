import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.music_bed import MusicBedError, MusicBedRenderer


class MusicBedRendererTests(unittest.TestCase):
    def test_multi_cue_bed_updates_timeline_and_writes_rights_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "track.wav"
            track.write_bytes(b"fake")
            timeline = root / "timeline_cuts.json"
            output = root / "music" / "music_bed.wav"
            timeline.write_text(
                json.dumps(
                    {
                        "clips": [
                            {
                                "cut_in_sec": 0,
                                "cut_out_sec": 5,
                                "story_role": "interview",
                            },
                            {
                                "cut_in_sec": 5,
                                "cut_out_sec": 10,
                                "story_role": "broll",
                            },
                        ],
                        "music_plan": {
                            "silence_regions": [
                                {
                                    "timeline_in_sec": 8,
                                    "timeline_out_sec": 9,
                                    "reason": "breath",
                                }
                            ],
                            "cues": [
                                {
                                    "cue_id": "M1",
                                    "file_name": str(track),
                                    "timeline_in_sec": 0,
                                    "timeline_out_sec": 10,
                                    "track_in_sec": 1,
                                    "track_out_sec": 11,
                                    "target_lufs": -24,
                                    "integrated_lufs": -18,
                                    "fade_in_sec": 1,
                                    "fade_out_sec": 1,
                                    "duck_under_dialogue_db": -9,
                                    "license": "user-confirmed rights",
                                    "source_url": "https://example.test/source",
                                    "sha256": "abc",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            def fake_run(command, **kwargs):
                rendered = Path(command[-1])
                rendered.parent.mkdir(parents=True, exist_ok=True)
                rendered.write_bytes(b"wav")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("src.music_bed.shutil.which", return_value="ffmpeg"), mock.patch(
                "src.music_bed.subprocess.run", side_effect=fake_run
            ) as run, mock.patch.object(
                MusicBedRenderer, "_measure_peak_db", return_value=-6.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_integrated_lufs", return_value=-23.0
            ):
                result = MusicBedRenderer(timeline, output).render()

            self.assertEqual(result, output.resolve())
            command = run.call_args.args[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("eval=frame", graph)
            self.assertIn("0.35481339", graph)
            self.assertIn("volume=0", graph)
            self.assertIn("loudnorm=I=-23:TP=-2:LRA=11", graph)
            updated = json.loads(timeline.read_text(encoding="utf-8"))
            self.assertEqual(updated["music_plan"]["bed_file"], str(output.resolve()))
            self.assertTrue(output.with_suffix(".audit.json").is_file())

    def test_silent_render_is_rejected_before_replacing_previous_bed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "track.wav"
            track.write_bytes(b"source")
            output = root / "music_bed.wav"
            output.write_bytes(b"previous-good-bed")
            timeline = root / "timeline.json"
            timeline.write_text(
                json.dumps(
                    {
                        "clips": [{"cut_in_sec": 0, "cut_out_sec": 2}],
                        "music_plan": {
                            "silence_regions": [],
                            "cues": [
                                {
                                    "file_name": str(track), "timeline_in_sec": 0,
                                    "timeline_out_sec": 2, "track_in_sec": 0,
                                    "track_out_sec": 2,
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"all-zero-wav")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("src.music_bed.shutil.which", return_value="ffmpeg"), mock.patch(
                "src.music_bed.subprocess.run", side_effect=fake_run
            ), mock.patch.object(
                MusicBedRenderer, "_measure_peak_db", return_value=float("-inf")
            ), mock.patch.object(
                MusicBedRenderer, "_measure_integrated_lufs", return_value=-23.0
            ):
                with self.assertRaises(MusicBedError):
                    MusicBedRenderer(timeline, output).render()

            self.assertEqual(output.read_bytes(), b"previous-good-bed")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg integration test")
    def test_real_ffmpeg_renders_two_overlapping_cues(self):
        ffmpeg = str(shutil.which("ffmpeg"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            for frequency, destination in ((220, first), (440, second)):
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"sine=frequency={frequency}:duration=4:sample_rate=48000",
                        "-c:a",
                        "pcm_s16le",
                        str(destination),
                    ],
                    check=True,
                )
            timeline = root / "timeline.json"
            output = root / "music_bed.wav"
            timeline.write_text(
                json.dumps(
                    {
                        "clips": [
                            {"cut_in_sec": 0, "cut_out_sec": 3, "story_role": "interview"},
                            {"cut_in_sec": 0, "cut_out_sec": 3, "story_role": "broll"},
                        ],
                        "music_plan": {
                            "silence_regions": [],
                            "cues": [
                                {
                                    "cue_id": "M1", "file_name": str(first),
                                    "timeline_in_sec": 0, "timeline_out_sec": 4,
                                    "track_in_sec": 0, "track_out_sec": 4,
                                    "fade_in_sec": 0.2, "fade_out_sec": 1,
                                    "target_lufs": -24, "integrated_lufs": -21,
                                    "duck_under_dialogue_db": -9,
                                },
                                {
                                    "cue_id": "M2", "file_name": str(second),
                                    "timeline_in_sec": 3, "timeline_out_sec": 6,
                                    "track_in_sec": 0, "track_out_sec": 3,
                                    "fade_in_sec": 1, "fade_out_sec": 0.2,
                                    "target_lufs": -24, "integrated_lufs": -21,
                                    "duck_under_dialogue_db": -9,
                                },
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = MusicBedRenderer(timeline, output, ffmpeg_path=ffmpeg).render()

            self.assertEqual(result, output.resolve())
            self.assertGreater(output.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
