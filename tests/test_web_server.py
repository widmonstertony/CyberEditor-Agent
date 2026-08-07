"""Tests for the dependency-free browser controller. / 零依赖浏览器控制器测试。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error as urllib_error
from urllib import request as urllib_request

from src.web_server import (
    CyberEditorHandler,
    CyberEditorHTTPServer,
    WorkflowManager,
    _is_loopback,
)


class WebServerTests(unittest.TestCase):
    """Validate web configuration and file confinement. / 校验网页配置与文件边界。"""

    def make_manager(self, root: Path) -> WorkflowManager:
        """Create a manager rooted in a temporary project. / 创建临时项目管理器。"""
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
        return WorkflowManager(root, "python.exe")

    def test_loopback_detection_is_fail_closed(self) -> None:
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("0.0.0.0"))
        self.assertFalse(_is_loopback("192.168.1.5"))

    def test_hosted_ui_declares_the_companion_as_loopback(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        app_source = (project_root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('targetAddressSpace: "loopback"', app_source)
        self.assertNotIn('targetAddressSpace: "local"', app_source)
        self.assertIn(
            'href="http://127.0.0.1:8765/"',
            (project_root / "web" / "index.html").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "--no-browser",
            (project_root / "launch_companion.command").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "--no-browser",
            (project_root / "launch_companion.bat").read_text(encoding="utf-8"),
        )

    def test_unknown_submission_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(Path(temporary))
            with self.assertRaisesRegex(ValueError, "Unknown option fields"):
                manager.build_options({"shell_command": "danger"})

    def test_full_flow_auto_fps_uses_first_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            with mock.patch("src.web_server.detect_media_fps", return_value=59.94006):
                options = manager.build_options(
                    {
                        "flow": "full",
                        "videos": [str(source)],
                        "video": str(source),
                        "fps_mode": "auto",
                        "project_fps": 25,
                        "ollama_model": "installed-model",
                    }
                )
            self.assertEqual(options.project_fps, 59.94006)

    def test_saved_settings_only_accept_known_workflow_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            manager.settings_path.write_text(
                json.dumps(
                    {
                        "flow": "director",
                        "num_ctx": 32768,
                        "unexpected_secret": "must-not-escape",
                    }
                ),
                encoding="utf-8",
            )
            settings = manager.settings()
            self.assertEqual(settings["flow"], "director")
            self.assertEqual(settings["num_ctx"], 32768)
            self.assertNotIn("unexpected_secret", settings)

    def test_output_paths_cannot_escape_active_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            active = root / "data" / "ui-run"
            active.mkdir(parents=True)
            allowed = active / "preview.mp4"
            allowed.write_bytes(b"preview")
            outside = root / "private.mov"
            outside.write_bytes(b"private")
            manager._active_data_dir = active

            self.assertEqual(manager.authorize_output_path(str(allowed)), allowed.resolve())
            with self.assertRaises(PermissionError):
                manager.authorize_output_path(str(outside))

    def test_output_listing_skips_asset_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            active = root / "data" / "ui-run"
            final = active / "final"
            assets = active / "assets"
            final.mkdir(parents=True)
            assets.mkdir()
            (final / "film.mov").write_bytes(b"film")
            (assets / "source.mp4").write_bytes(b"source")
            manager._active_data_dir = active

            outputs = manager.output_files()
            self.assertEqual([item["name"] for item in outputs], ["film.mov"])

    def test_http_api_requires_token_and_json_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            static_root = root / "web"
            static_root.mkdir()
            (static_root / "index.html").write_text("ok", encoding="utf-8")
            server = CyberEditorHTTPServer(
                ("127.0.0.1", 0),
                CyberEditorHandler,
                manager,
                static_root,
                "0123456789abcdef",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(urllib_error.HTTPError) as unauthorized:
                    urllib_request.urlopen(base_url + "/api/config", timeout=3)
                self.assertEqual(unauthorized.exception.code, 401)
                unauthorized.exception.close()

                request = urllib_request.Request(
                    base_url + "/api/workflow/stop",
                    data=b"{}",
                    method="POST",
                    headers={
                        "X-CyberEditor-Token": "0123456789abcdef",
                        "Content-Type": "text/plain",
                    },
                )
                with self.assertRaises(urllib_error.HTTPError) as wrong_type:
                    urllib_request.urlopen(request, timeout=3)
                self.assertEqual(wrong_type.exception.code, 400)
                wrong_type.exception.close()

                request = urllib_request.Request(
                    base_url + "/api/workflow/stop",
                    data=b"{}",
                    method="POST",
                    headers={
                        "X-CyberEditor-Token": "0123456789abcdef",
                        "Content-Type": "application/json",
                    },
                )
                with urllib_request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_hosted_ui_cors_and_private_network_preflight_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self.make_manager(root)
            static_root = root / "web"
            static_root.mkdir()
            (static_root / "index.html").write_text("ok", encoding="utf-8")
            server = CyberEditorHTTPServer(
                ("127.0.0.1", 0),
                CyberEditorHandler,
                manager,
                static_root,
                "",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                request = urllib_request.Request(
                    base_url + "/api/capabilities",
                    method="OPTIONS",
                    headers={
                        "Origin": "https://tonytan.me",
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Private-Network": "true",
                    },
                )
                with urllib_request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 204)
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Origin"],
                        "https://tonytan.me",
                    )
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Private-Network"],
                        "true",
                    )

                request = urllib_request.Request(
                    base_url + "/api/config",
                    headers={"Origin": "https://attacker.example"},
                )
                with self.assertRaises(urllib_error.HTTPError) as forbidden:
                    urllib_request.urlopen(request, timeout=3)
                self.assertEqual(forbidden.exception.code, 403)
                forbidden.exception.close()

                request = urllib_request.Request(
                    base_url + "/api/config",
                    headers={"Origin": "https://tonytan.me"},
                )
                with urllib_request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Origin"],
                        "https://tonytan.me",
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
