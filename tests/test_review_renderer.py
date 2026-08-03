"""Tests for FFmpeg review graph construction without running FFmpeg."""

import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.review_renderer import RenderClip, ReviewRenderer


class ReviewRendererTests(unittest.TestCase):
    def test_graph_uses_hard_cut_transition_and_audio_cleanup(self):
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
                RenderClip(str(media[2]), 0, 3, "cut", 0, "none"),
            ]

            with mock.patch.object(renderer, "_has_audio", return_value=True):
                command, graph, duration = renderer.build_command(
                    "ffmpeg", "ffprobe", 25.0, clips
                )

            self.assertIn("concat=n=2:v=1:a=0", graph)
            self.assertIn("xfade=transition=fade:duration=0.5", graph)
            self.assertIn("afftdn=nr=18:nf=-30", graph)
            self.assertIn("__FILTER_SCRIPT__", command)
            self.assertAlmostEqual(duration, 11.5)


if __name__ == "__main__":
    unittest.main()
