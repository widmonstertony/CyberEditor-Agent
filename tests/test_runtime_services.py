"""Tests for dependency-free Windows runtime discovery and startup."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib import error as urllib_error

from src import runtime_services


class RuntimeServicesTests(unittest.TestCase):
    """Validate custom-drive discovery and Ollama auto-start control flow."""

    def test_find_resolve_scans_non_system_drive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            drive = Path(directory)
            executable = (
                drive
                / "Program Files"
                / "Blackmagic Design"
                / "DaVinci Resolve"
                / "Resolve.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()
            with (
                mock.patch.object(
                    runtime_services, "_fixed_drive_roots", return_value=[drive]
                ),
                mock.patch.object(
                    runtime_services,
                    "_resolve_registry_candidates",
                    return_value=[],
                ),
                mock.patch.object(
                    runtime_services.shutil, "which", return_value=None
                ),
            ):
                result = runtime_services.find_resolve_executable()
            self.assertEqual(result, executable.resolve())

    def test_resolve_edition_uses_matching_registered_product(self) -> None:
        executable = Path(
            r"D:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
        )
        with (
            mock.patch.object(
                runtime_services, "_windows_file_product_name", return_value=""
            ),
            mock.patch.object(
                runtime_services,
                "_resolve_registry_installations",
                return_value=[(executable, "DaVinci Resolve")],
            ),
        ):
            self.assertEqual(
                runtime_services.detect_resolve_edition(executable), "free"
            )

        with (
            mock.patch.object(
                runtime_services, "_windows_file_product_name", return_value=""
            ),
            mock.patch.object(
                runtime_services,
                "_resolve_registry_installations",
                return_value=[(executable, "DaVinci Resolve Studio")],
            ),
        ):
            self.assertEqual(
                runtime_services.detect_resolve_edition(executable), "studio"
            )

    def test_unknown_resolve_layout_is_not_falsely_blocked(self) -> None:
        executable = Path(r"Z:\Portable Resolve\Resolve.exe")
        with (
            mock.patch.object(
                runtime_services, "_windows_file_product_name", return_value=""
            ),
            mock.patch.object(
                runtime_services,
                "_resolve_registry_installations",
                return_value=[],
            ),
        ):
            self.assertEqual(
                runtime_services.detect_resolve_edition(executable), "unknown"
            )

    def test_find_ollama_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            install = local / "Programs" / "Ollama"
            install.mkdir(parents=True)
            cli = install / "ollama.exe"
            app = install / "ollama app.exe"
            cli.touch()
            app.touch()
            with (
                mock.patch.dict(
                    runtime_services.os.environ,
                    {"LOCALAPPDATA": str(local)},
                    clear=False,
                ),
                mock.patch.object(
                    runtime_services.shutil, "which", return_value=None
                ),
            ):
                found_cli, found_app = (
                    runtime_services.find_ollama_executables()
                )
            self.assertEqual(found_cli, cli.resolve())
            self.assertEqual(found_app, app.resolve())

    def test_ready_ollama_is_not_relaunched(self) -> None:
        models = [{"name": "qwen3.5:9b-q8_0", "size": 1}]
        with (
            mock.patch.object(
                runtime_services, "fetch_ollama_models", return_value=models
            ),
            mock.patch.object(
                runtime_services, "_launch_detached"
            ) as launch,
        ):
            result, started = runtime_services.ensure_ollama_service(
                "http://127.0.0.1:11434"
            )
        self.assertEqual(result, models)
        self.assertFalse(started)
        launch.assert_not_called()

    def test_stopped_ollama_tray_app_is_started(self) -> None:
        models = [{"name": "qwen2.5:3b", "size": 1}]
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(
                runtime_services,
                "fetch_ollama_models",
                side_effect=[
                    urllib_error.URLError("offline"),
                    models,
                ],
            ),
            mock.patch.object(
                runtime_services,
                "find_ollama_executables",
                return_value=(Path("ollama.exe"), Path("ollama app.exe")),
            ),
            mock.patch.object(
                runtime_services, "_launch_detached", return_value=process
            ) as launch,
        ):
            result, started = runtime_services.ensure_ollama_service(
                "http://localhost:11434"
            )
        self.assertEqual(result, models)
        self.assertTrue(started)
        launch.assert_called_once_with(["ollama app.exe"])

    def test_remote_ollama_is_never_auto_started(self) -> None:
        with mock.patch.object(
            runtime_services,
            "fetch_ollama_models",
            side_effect=urllib_error.URLError("offline"),
        ):
            with self.assertRaises(runtime_services.RuntimeServiceError):
                runtime_services.ensure_ollama_service(
                    "http://192.0.2.10:11434"
                )


if __name__ == "__main__":
    unittest.main()
