"""Tests for deterministic multi-video discovery and proxy matching."""

from pathlib import Path
import tempfile
import unittest

from src.media_manifest import discover_video_files, match_proxy_files


class MediaManifestTests(unittest.TestCase):
    def test_folder_discovery_is_recursive_natural_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            video_10 = root / "clip10.mp4"
            video_2 = root / "clip2.mp4"
            nested_video = nested / "clip3.mov"
            for path in (video_10, video_2, nested_video):
                path.touch()
            (root / "ignore.txt").touch()

            result = discover_video_files([video_2], root)

            self.assertEqual(result[0], video_2.resolve())
            self.assertEqual(len(result), 3)
            self.assertEqual(result[1:], [video_10.resolve(), nested_video.resolve()])

    def test_proxy_folder_matches_by_stem_and_falls_back_to_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proxy_root = root / "proxies"
            proxy_root.mkdir()
            source_a = root / "A.mp4"
            source_b = root / "B.mp4"
            proxy_a = proxy_root / "A.mov"
            for path in (source_a, source_b, proxy_a):
                path.touch()

            result = match_proxy_files(
                [source_a.resolve(), source_b.resolve()],
                proxy_folder=proxy_root,
            )

            self.assertEqual(result[source_a.resolve()], proxy_a.resolve())
            self.assertEqual(result[source_b.resolve()], source_b.resolve())


if __name__ == "__main__":
    unittest.main()
