"""Tests for FFmpeg review graph construction without running FFmpeg."""

import logging
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.frame_edl import build_frame_edl
from src.review_renderer import RenderClip, ReviewRenderer
from src.color_pipeline import decode_slog3, ensure_sony_pp8_display_lut


class ReviewRendererTests(unittest.TestCase):
    def test_graph_defers_unimplemented_transition_to_frame_safe_hard_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = []
            for name in ("a.mp4", "b.mp4", "c.mp4"):
                path = root / name
                path.touch()
                media.append(path)
            renderer = ReviewRenderer(
                root / "plan.json",
                root / "review.mp4",
                logger=logging.getLogger("test.review"),
            )
            clips = [
                RenderClip(str(media[0]), 0, 4, "cut", 0, "light"),
                RenderClip(
                    str(media[1]), 1, 6, "cross_dissolve", 0.5, "strong"
                ),
                RenderClip(
                    str(media[2]), 0, 3, "cut", 0, "none",
                    volume_db=-4.5,
                ),
            ]

            with mock.patch.object(renderer, "_has_audio", return_value=True):
                command, graph, duration = renderer.build_command(
                    "ffmpeg", "ffprobe", 25.0, clips
                )

            self.assertIn("concat=n=2:v=1:a=0", graph)
            self.assertNotIn("xfade=", graph)
            self.assertNotIn("acrossfade=", graph)
            self.assertEqual(graph.count("settb=AVTB"), len(clips))
            self.assertIn("afftdn=nr=18:nf=-30", graph)
            self.assertIn("volume=-4.5dB", graph)
            self.assertIn("__FILTER_SCRIPT__", command)
            self.assertAlmostEqual(duration, 12.0)

    def test_sony_pp8_lut_is_generated_without_third_party_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = ensure_sony_pp8_display_lut(
                Path(temporary) / "sony_pp8.cube", size=5
            )
            text = path.read_text(encoding="ascii")

            self.assertIn("LUT_3D_SIZE 5", text)
            self.assertAlmostEqual(decode_slog3(420 / 1023), 0.18, places=4)

    def test_preconformed_music_bed_is_not_looped_or_reprocessed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "a.mp4"
            bed = root / "music_bed.wav"
            media.touch()
            bed.touch()
            renderer = ReviewRenderer(root / "plan.json", root / "review.mp4")
            renderer.music_plan = {"bed_file": str(bed)}

            with mock.patch.object(renderer, "_has_audio", return_value=True):
                command, graph, _ = renderer.build_command(
                    "ffmpeg",
                    "ffprobe",
                    25.0,
                    [RenderClip(str(media), 0, 4, "cut", 0, "none")],
                )

            self.assertNotIn("-stream_loop", command)
            self.assertIn(str(bed.resolve()), command)
            self.assertIn("[musicbed]", graph)
            self.assertNotIn("afade=t=in", graph)

    def test_director_color_bible_becomes_executable_filter(self):
        renderer = ReviewRenderer("plan.json", "review.mp4")
        clip = RenderClip(
            "a.mp4", 0, 3,
            creative_grade={
                "palette": "cool_moonlight", "exposure_ev": -0.1,
                "contrast": 1.08, "saturation": 0.92, "warmth": -0.25,
            },
        )

        value = renderer._creative_grade_filter(clip)

        self.assertIn("colorbalance=", value)
        self.assertIn("eq=contrast=1.08:saturation=0.92", value)
        self.assertIn("exposure=exposure=-0.1", value)

    def test_graphics_plan_becomes_executable_drawtext_overlay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "a.mp4"
            media.touch()
            renderer = ReviewRenderer(root / "plan.json", root / "review.mp4")
            renderer.graphics_plan = {
                "items": [
                    {
                        "kind": "title_card",
                        "timeline_in_sec": 0.2,
                        "timeline_out_sec": 2.8,
                        "text": "NIGHT SHIFT",
                        "subtitle": "Ready is a ritual",
                        "style": "kinetic",
                    }
                ]
            }

            with mock.patch.object(renderer, "_has_audio", return_value=True):
                _, graph, _ = renderer.build_command(
                    "ffmpeg", "ffprobe", 25.0,
                    [RenderClip(str(media), 0, 4, "cut", 0, "none")],
                )

            self.assertIn("drawtext=", graph)
            self.assertIn("NIGHT SHIFT", graph)
            self.assertIn("Ready is a ritual", graph)
            self.assertIn("[vg0]", graph)

    def test_transparent_graphics_filter_writes_alpha_and_scales_for_4k(self):
        renderer = ReviewRenderer(
            "plan.json", "overlay.mov", width=3840, height=2160
        )
        graph = renderer._graphics_filter(
            {
                "kind": "chapter", "timeline_in_sec": 0,
                "timeline_out_sec": 2, "text": "CHAPTER",
                "subtitle": "A deliberate turn", "style": "kinetic",
            },
            "base",
            "graphic",
            transparent_canvas=True,
        )

        self.assertIn("replace=1", graph)
        self.assertIn("fontsize=96", graph)
        self.assertIn("x=200", graph)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_transparent_graphic_has_nonzero_alpha_plane(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "graphic.mov"
            script = root / "graphic.fffilter"
            renderer = ReviewRenderer(
                root / "plan.json", output, width=640, height=360
            )
            graph = renderer._graphics_filter(
                {
                    "kind": "title_card", "timeline_in_sec": 0,
                    "timeline_out_sec": 1, "text": "ALPHA",
                    "subtitle": "VISIBLE", "style": "bold_cinematic",
                },
                "base",
                "graphic",
                transparent_canvas=True,
            )
            script.write_text(
                "[0:v]format=yuva444p10le[base];\n" + graph,
                encoding="utf-8",
            )
            subprocess.run(
                [
                    str(shutil.which("ffmpeg")), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black@0:s=640x360:r=25:d=1,format=rgba",
                    "-filter_complex_script", str(script), "-map", "[graphic]",
                    "-frames:v", "25", "-c:v", "prores_ks", "-profile:v", "4",
                    "-pix_fmt", "yuva444p10le", str(output),
                ],
                check=True,
            )
            probe = subprocess.run(
                [
                    str(shutil.which("ffmpeg")), "-hide_banner", "-loglevel", "info",
                    "-ss", "0.5", "-i", str(output),
                    "-vf", "alphaextract,signalstats,metadata=print",
                    "-frames:v", "1", "-f", "null", "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            values = re.findall(
                r"lavfi\.signalstats\.YAVG=([0-9.]+)", probe.stderr
            )

            self.assertTrue(values)
            self.assertGreater(max(float(value) for value in values), 0.05)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_ffmpeg_renders_unicode_title_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "review.mp4"
            subprocess.run(
                [
                    str(shutil.which("ffmpeg")), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x142033:s=640x360:r=25:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
                ],
                check=True,
            )
            plan = root / "timeline.json"
            plan_payload = {
                        "project_fps": 25,
                        "clips": [
                            {
                                "clip_id": 1,
                                "file_name": str(source), "cut_in_sec": 0,
                                "cut_out_sec": 2, "audio_cleanup": "none",
                            }
                        ],
                        "graphics_plan": {
                            "strategy": "clarify premise",
                            "items": [
                                {
                                    "kind": "title_card", "timeline_in_sec": 0.1,
                                    "timeline_out_sec": 1.8, "text": "夜行集结",
                                    "subtitle": "NIGHT SHIFT", "style": "bold_cinematic",
                                }
                            ],
                        },
                    }
            plan_payload["frame_edl"] = build_frame_edl(
                plan_payload, source_fps_overrides=[25]
            )
            plan.write_text(
                json.dumps(plan_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            result = ReviewRenderer(plan, output, 640, 360).run()

            self.assertEqual(result, output.resolve())
            self.assertGreater(output.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
