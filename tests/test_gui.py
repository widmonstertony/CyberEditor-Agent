"""Tests for the dependency-free UI command builder."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.gui import (
    WorkflowOptions,
    build_runtime_environment,
    console_python_executable,
    detect_torch_runtime,
    detect_system_theme,
    enable_windows_high_dpi,
    get_primary_work_area,
    parse_frame_rate,
    recommend_automatic_settings,
)
from src.ui_i18n import resolve_language, translate


class WorkflowOptionsTests(unittest.TestCase):
    def test_pythonw_is_replaced_by_console_python_for_child_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pythonw = root / "pythonw.exe"
            python = root / "python.exe"
            pythonw.touch()
            python.touch()

            self.assertEqual(
                console_python_executable(str(pythonw)), str(python.resolve())
            )

    def test_torch_detection_uses_console_python_from_gui_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pythonw = root / "pythonw.exe"
            python = root / "python.exe"
            pythonw.touch()
            python.touch()
            completed = mock.Mock(
                stdout=(
                    '{"torch_available":true,"torch_version":"test+cu",'
                    '"torch_cuda":true,"torch_device":"Test GPU"}\n'
                )
            )

            with mock.patch("src.gui.sys.executable", str(pythonw)), mock.patch(
                "src.gui.subprocess.run", return_value=completed
            ) as run:
                runtime = detect_torch_runtime()

            self.assertTrue(runtime["torch_cuda"])
            self.assertEqual(
                run.call_args.args[0][0], str(python.resolve())
            )

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

    def test_multi_video_and_folder_inputs_build_batch_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            first = root / "first.mp4"
            second = root / "second.mov"
            folder_video = folder / "third.mkv"
            for path in (first, second, folder_video):
                path.touch()
            options = WorkflowOptions(
                videos=[str(first), str(second)],
                input_folder=str(folder),
                ollama_model="qwen3.5:test",
                render_preview=True,
            )

            command = options.build_command("python.exe", root)

            self.assertEqual(command.count("--video"), 2)
            self.assertIn("--input-folder", command)
            self.assertNotIn("--skip-preview", command)

    def test_director_intent_color_and_music_flags_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "source.mp4"
            music = root / "music"
            video.touch()
            music.mkdir()
            options = WorkflowOptions(
                video=str(video),
                creative_brief="A chronological behind-the-scenes short",
                target_duration_sec=75,
                camera_profile="sony_pp8_slog3_sgamut3cine",
                music_folder=str(music),
            )

            command = options.build_command("python.exe", root)

            self.assertIn("--creative-brief", command)
            self.assertIn("--target-duration-sec", command)
            self.assertIn("--camera-profile", command)
            self.assertIn("--music-folder", command)

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

    def test_high_dpi_helper_returns_a_mode(self) -> None:
        mode = enable_windows_high_dpi()
        self.assertIn(
            mode,
            {
                "per-monitor-v2",
                "per-monitor",
                "system",
                "unavailable",
                "platform-default",
            },
        )

    def test_work_area_is_positive(self) -> None:
        left, top, width, height = get_primary_work_area(1920, 1080)
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)

    def test_auto_profile_is_conservative_on_low_memory(self) -> None:
        recommendation = recommend_automatic_settings(
            {
                "ram_gb": 16,
                "vram_gb": 0,
                "cpu_threads": 8,
            },
            [
                {"name": "small:latest", "size": 2 * 1024**3},
                {"name": "large:latest", "size": 20 * 1024**3},
            ],
        )
        self.assertEqual(recommendation["profile"], "conservative")
        self.assertEqual(recommendation["whisper_model"], "base")
        self.assertEqual(recommendation["num_ctx"], 4096)
        self.assertEqual(recommendation["ollama_model"], "small:latest")

    def test_auto_profile_uses_capable_installed_model(self) -> None:
        recommendation = recommend_automatic_settings(
            {
                "ram_gb": 64,
                "vram_gb": 16,
                "cpu_threads": 24,
                "torch_cuda": True,
            },
            [
                {"name": "qwen:3b", "size": 2 * 1024**3},
                {"name": "qwen:32b", "size": 20 * 1024**3},
            ],
        )
        self.assertEqual(recommendation["profile"], "performance")
        self.assertEqual(recommendation["whisper_model"], "large-v3")
        self.assertEqual(recommendation["num_ctx"], 16384)
        self.assertEqual(recommendation["ollama_model"], "qwen:32b")

    def test_nominal_64_gib_windows_machine_gets_16k_context(self) -> None:
        recommendation = recommend_automatic_settings(
            {
                "ram_gb": 63.8,
                "vram_gb": 16,
                "cpu_threads": 16,
                "torch_cuda": True,
            },
            [
                {
                    "name": "qwen3.5:35b-a3b",
                    "size": 23 * 1024**3,
                }
            ],
        )
        self.assertEqual(recommendation["profile"], "performance")
        self.assertEqual(recommendation["num_ctx"], 16384)

    def test_auto_profile_prefers_editing_quality_over_file_size(self) -> None:
        recommendation = recommend_automatic_settings(
            {
                "ram_gb": 64,
                "vram_gb": 16,
                "cpu_threads": 16,
                "torch_cuda": True,
            },
            [
                {"name": "generic:20b", "size": 14 * 1024**3},
                {
                    "name": "qwen3.5:9b-q8_0",
                    "size": 11 * 1024**3,
                },
                {"name": "qwen2.5:3b", "size": 2 * 1024**3},
            ],
        )
        self.assertEqual(
            recommendation["ollama_model"], "qwen3.5:9b-q8_0"
        )
        self.assertEqual(recommendation["chunk_minutes"], 10.0)

    def test_auto_profile_prefers_qwen36_dense_q8_for_both_roles(self) -> None:
        recommendation = recommend_automatic_settings(
            {"ram_gb": 64, "vram_gb": 16, "cpu_threads": 16, "torch_cuda": True},
            [
                {"name": "qwen3.5:35b-a3b", "size": 23 * 1024**3},
                {"name": "qwen2.5:72b-instruct-q5_K_M", "size": 54 * 1024**3},
                {"name": "qwen3.6:27b-mtp-q8_0", "size": 30 * 1024**3},
            ],
        )

        self.assertEqual(
            recommendation["ollama_model"], "qwen3.6:27b-mtp-q8_0"
        )
        self.assertEqual(
            recommendation["director_model"],
            "qwen3.6:27b-mtp-q8_0",
        )

    def test_auto_profile_does_not_assume_pytorch_cuda(self) -> None:
        recommendation = recommend_automatic_settings(
            {
                "ram_gb": 64,
                "vram_gb": 16,
                "cpu_threads": 16,
                "torch_cuda": False,
            },
            [{"name": "qwen:3b", "size": 2 * 1024**3}],
        )
        self.assertEqual(recommendation["whisper_model"], "small")
        self.assertEqual(recommendation["whisper_device"], "auto")

    def test_system_theme_has_supported_value(self) -> None:
        self.assertIn(detect_system_theme(), {"light", "dark"})

    def test_ntsc_frame_rate_is_parsed_exactly(self) -> None:
        self.assertAlmostEqual(
            parse_frame_rate("30000/1001"),
            29.97003,
            places=5,
        )

    def test_interface_translations_cover_chinese_and_english(self) -> None:
        self.assertEqual(translate("zh", "start"), "开始串行工作流")
        self.assertEqual(translate("en", "start"), "Start serial workflow")

    def test_explicit_interface_language_does_not_depend_on_system(self) -> None:
        self.assertEqual(resolve_language("zh"), "zh")
        self.assertEqual(resolve_language("en"), "en")


if __name__ == "__main__":
    unittest.main()
