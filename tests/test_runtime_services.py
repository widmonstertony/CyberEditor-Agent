"""Tests for dependency-free Windows runtime discovery and startup."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib import error as urllib_error

from src import runtime_services


class RuntimeServicesTests(unittest.TestCase):
    """Validate custom-drive discovery and Ollama auto-start control flow."""

    def test_find_resolve_uses_windows_registered_start_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "custom-drive" / "Resolve.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with (
                mock.patch.object(
                    runtime_services,
                    "get_resolve_registration",
                    return_value={"installed": True, "version": "21.0.00047"},
                ),
                mock.patch.object(
                    runtime_services,
                    "_resolve_registry_candidates",
                    return_value=[],
                ),
                mock.patch.object(
                    runtime_services,
                    "_resolve_start_app_candidates",
                    return_value=[executable],
                ),
                mock.patch.object(
                    runtime_services.shutil, "which", return_value=None
                ),
                mock.patch.dict(
                    runtime_services.os.environ,
                    {"RESOLVE_SCRIPT_LIB": ""},
                    clear=False,
                ),
            ):
                result = runtime_services.find_resolve_executable()
            self.assertEqual(result, executable.resolve())

    def test_resolve_registration_reads_official_blackmagic_keys(self) -> None:
        fake_winreg = mock.Mock()
        fake_winreg.HKEY_LOCAL_MACHINE = object()
        fake_winreg.HKEY_CURRENT_USER = object()
        with (
            mock.patch.object(
                runtime_services, "winreg", fake_winreg
            ),
            mock.patch.object(
                runtime_services.os, "name", "nt"
            ),
            mock.patch.object(
                runtime_services,
                "_resolve_registry_value",
                side_effect=["21.0.00047", 1],
            ) as read_value,
        ):
            registration = runtime_services.get_resolve_registration()

        self.assertTrue(registration["installed"])
        self.assertTrue(registration["user_registered"])
        self.assertEqual(registration["version"], "21.0.00047")
        self.assertEqual(read_value.call_count, 2)

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
