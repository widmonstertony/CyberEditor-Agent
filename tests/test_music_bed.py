import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.frame_edl import build_frame_edl
from src.music_bed import MusicBedError, MusicBedRenderer


def framed(payload, source_rates=None):
    """Attach a deterministic canonical record-frame schedule. / 附加确定性帧表。"""
    payload["project_fps"] = 25
    for index, clip in enumerate(payload["clips"], start=1):
        clip.setdefault("clip_id", index)
        clip.setdefault("file_name", f"source-{index}.mp4")
    payload["frame_edl"] = build_frame_edl(
        payload,
        source_fps_overrides=source_rates or [25] * len(payload["clips"]),
    )
    return payload


class MusicBedRendererTests(unittest.TestCase):
    def test_overlapping_cues_execute_matched_crossfade_and_audit_isolated_stems(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "track.wav"
            track.write_bytes(b"source")
            timeline = root / "timeline.json"
            output = root / "music_bed.wav"
            timeline.write_text(
                json.dumps(
                    framed({
                        "clips": [{"cut_in_sec": 0, "cut_out_sec": 6}],
                        "music_plan": {
                            "silence_regions": [],
                            "cues": [
                                {
                                    "cue_id": "M1", "file_name": str(track),
                                    "timeline_in_sec": 0, "timeline_out_sec": 4,
                                    "track_in_sec": 0, "track_out_sec": 4,
                                    "fade_in_sec": 0, "fade_out_sec": 0,
                                    "crossfade_sec": 1,
                                },
                                {
                                    "cue_id": "M2", "file_name": str(track),
                                    "timeline_in_sec": 3, "timeline_out_sec": 6,
                                    "track_in_sec": 4, "track_out_sec": 7,
                                    "fade_in_sec": 0, "fade_out_sec": 0,
                                },
                            ],
                        },
                    })
                ),
                encoding="utf-8",
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"wav")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("src.music_bed.shutil.which", return_value="ffmpeg"), mock.patch(
                "src.music_bed.subprocess.run", side_effect=fake_run
            ) as run, mock.patch.object(
                MusicBedRenderer, "_probe_duration", side_effect=[10.0, 10.0, 6.0]
            ), mock.patch.object(
                MusicBedRenderer, "_measure_window_peak_db", return_value=-8.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_peak_db", return_value=-6.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_integrated_lufs", return_value=-23.0
            ):
                MusicBedRenderer(timeline, output).render()

            main_command = run.call_args.args[0]
            graph = main_command[main_command.index("-filter_complex") + 1]
            self.assertIn("afade=t=out:st=3.000000:d=1.000000", graph)
            self.assertIn("afade=t=in:st=0:d=1.000000", graph)
            audit = json.loads(
                output.with_suffix(".audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["cues"][0]["crossfade_out_executed_sec"], 1.0)
            self.assertEqual(audit["cues"][1]["crossfade_in_executed_sec"], 1.0)
            self.assertEqual(audit["cues"][0]["crossfade_execution"], "executed_overlap")
            self.assertEqual(audit["cues"][0]["rendered_peak_dbfs"], -6.0)

    def test_multi_cue_bed_updates_timeline_and_writes_rights_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "track.wav"
            track.write_bytes(b"fake")
            timeline = root / "timeline_cuts.json"
            output = root / "music" / "music_bed.wav"
            timeline.write_text(
                json.dumps(
                    framed({
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
                    })
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
                MusicBedRenderer, "_probe_duration", side_effect=[20.0, 10.0]
            ), mock.patch.object(
                MusicBedRenderer, "_measure_window_peak_db", return_value=-8.0
            ), mock.patch.object(
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
                    framed({
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
                    })
                ),
                encoding="utf-8",
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"all-zero-wav")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("src.music_bed.shutil.which", return_value="ffmpeg"), mock.patch(
                "src.music_bed.subprocess.run", side_effect=fake_run
            ), mock.patch.object(
                MusicBedRenderer, "_probe_duration", side_effect=[2.0, 2.0]
            ), mock.patch.object(
                MusicBedRenderer, "_measure_window_peak_db", return_value=-8.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_peak_db", return_value=float("-inf")
            ), mock.patch.object(
                MusicBedRenderer, "_measure_integrated_lufs", return_value=-23.0
            ):
                with self.assertRaises(MusicBedError):
                    MusicBedRenderer(timeline, output).render()

            self.assertEqual(output.read_bytes(), b"previous-good-bed")

    def test_same_source_cues_are_independently_trimmed_without_post_delay_atrim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "one-track.wav"
            track.write_bytes(b"source")
            timeline = root / "timeline.json"
            output = root / "bed.wav"
            timeline.write_text(
                json.dumps(
                    framed({
                        "clips": [{"cut_in_sec": 0, "cut_out_sec": 8}],
                        "music_plan": {
                            "silence_regions": [],
                            "cues": [
                                {
                                    "cue_id": "M1", "file_name": str(track),
                                    "timeline_in_sec": 0, "timeline_out_sec": 3,
                                    "track_in_sec": 1, "track_out_sec": 4,
                                },
                                {
                                    "cue_id": "M2", "file_name": str(track),
                                    "timeline_in_sec": 5, "timeline_out_sec": 8,
                                    "track_in_sec": 9, "track_out_sec": 12,
                                },
                            ],
                        },
                    })
                ),
                encoding="utf-8",
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"wav")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("src.music_bed.shutil.which", return_value="ffmpeg"), mock.patch(
                "src.music_bed.subprocess.run", side_effect=fake_run
            ) as run, mock.patch.object(
                MusicBedRenderer, "_probe_duration", side_effect=[20.0, 20.0, 8.0]
            ), mock.patch.object(
                MusicBedRenderer, "_measure_window_peak_db", return_value=-8.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_peak_db", return_value=-6.0
            ), mock.patch.object(
                MusicBedRenderer, "_measure_integrated_lufs", return_value=-23.0
            ):
                MusicBedRenderer(timeline, output).render()

            command = run.call_args.args[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertEqual(command.count(str(track.resolve())), 2)
            self.assertIn("[1:a:0]atrim=start=1.000000:duration=3.000000", graph)
            self.assertIn("[2:a:0]atrim=start=9.000000:duration=3.000000", graph)
            self.assertIn("adelay=5000|5000[cue1]", graph)
            self.assertNotIn("adelay=5000|5000,atrim", graph)

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
                    framed({
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
                    })
                ),
                encoding="utf-8",
            )

            result = MusicBedRenderer(timeline, output, ffmpeg_path=ffmpeg).render()

            self.assertEqual(result, output.resolve())
            self.assertGreater(output.stat().st_size, 1000)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg integration test")
    def test_real_ffmpeg_keeps_late_second_cue_from_same_source_audible(self):
        ffmpeg = str(shutil.which("ffmpeg"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "source.wav"
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=12:sample_rate=48000",
                    "-c:a", "pcm_s16le", str(track),
                ],
                check=True,
            )
            timeline = root / "timeline.json"
            output = root / "music_bed.wav"
            timeline.write_text(
                json.dumps(
                    framed({
                        "clips": [{"cut_in_sec": 0, "cut_out_sec": 6}],
                        "music_plan": {
                            "silence_regions": [],
                            "cues": [
                                {
                                    "cue_id": "M1", "file_name": str(track),
                                    "timeline_in_sec": 0, "timeline_out_sec": 2,
                                    "track_in_sec": 0, "track_out_sec": 2,
                                    "fade_in_sec": 0.1, "fade_out_sec": 0.1,
                                },
                                {
                                    "cue_id": "M2", "file_name": str(track),
                                    "timeline_in_sec": 4, "timeline_out_sec": 6,
                                    "track_in_sec": 7, "track_out_sec": 9,
                                    "fade_in_sec": 0.1, "fade_out_sec": 0.1,
                                },
                            ],
                        },
                    })
                ),
                encoding="utf-8",
            )

            renderer = MusicBedRenderer(timeline, output, ffmpeg_path=ffmpeg)
            renderer.render()

            self.assertAlmostEqual(renderer._probe_duration(output), 6.0, delta=0.03)
            self.assertGreater(renderer._measure_window_peak_db(output, 4.0, 2.0), -55.0)
            audit = json.loads(output.with_suffix(".audit.json").read_text(encoding="utf-8"))
            self.assertGreater(audit["cues"][1]["rendered_peak_dbfs"], -55.0)


if __name__ == "__main__":
    unittest.main()
