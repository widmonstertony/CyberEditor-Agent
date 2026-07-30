"""Tests for the dependency-free UI command builder."""

import os
from pathlib import Path
import tempfile
import unittest

from src.gui import WorkflowOptions, build_runtime_environment


class WorkflowOptionsTests(unittest.TestCase):
    def test_full_workflow_builds_expected_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "source.mp4"
            video.touch()
            options = WorkflowOptions(
                video=str(video),
                proxy=str(video),
                data_dir="data/run-one",
                ollama_model="qwen2.5:3b",
                skip_resolve=True,
            )

            command = options.build_command("python.exe", root)

            self.assertEqual(command[0], "python.exe")
            self.assertIn("--video", command)
            self.assertIn("--proxy", command)
            self.assertIn("--skip-resolve", command)
            self.assertNotIn("--skip-extraction", command)

    def test_resolve_only_requires_timeline_and_enables_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data" / "resume"
            data_dir.mkdir(parents=True)
            (data_dir / "timeline_cuts.json").write_text("{}", encoding="utf-8")
            options = WorkflowOptions(
                data_dir=str(data_dir),
                flow="resolve",
                skip_resolve=False,
            )

            command = options.build_command("python.exe", root)

            self.assertIn("--skip-extraction", command)
            self.assertIn("--skip-director", command)
            self.assertNotIn("--skip-resolve", command)

    def test_invalid_chunk_size_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "source.mp4"
            video.touch()
            options = WorkflowOptions(video=str(video), chunk_minutes=9.0)

            with self.assertRaisesRegex(ValueError, "10"):
                options.build_command("python.exe", root)

    def test_runtime_environment_preserves_current_path(self) -> None:
        environment = build_runtime_environment()
        self.assertIn("PATH", environment)
        self.assertIn(os.environ.get("PATH", ""), environment["PATH"])
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")


if __name__ == "__main__":
    unittest.main()
