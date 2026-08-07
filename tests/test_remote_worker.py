"""Tests for the outbound local worker. / 出站本机 Worker 测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import request as urllib_request

from src.control_plane import ControlPlaneHandler, ControlPlaneServer, ControlPlaneStore
from src.remote_worker import ControlPlaneClient, RemoteWorker


TOKEN = "worker-token-that-is-long-enough"


class FakeClient:
    """Capture reports without network access. / 在不访问网络的情况下捕获上报。"""

    worker_id = "edit-pc"

    def __init__(self) -> None:
        self.reports = []

    def report(self, job_id, payload):
        self.reports.append((job_id, payload))
        return False


class RemoteWorkerTests(unittest.TestCase):
    """Validate transport safety and local command execution. / 校验传输安全与本地命令执行。"""

    def test_plain_http_is_only_allowed_for_loopback_without_override(self) -> None:
        ControlPlaneClient("http://127.0.0.1:8765", TOKEN, "edit-pc")
        with self.assertRaisesRegex(ValueError, "require HTTPS"):
            ControlPlaneClient("http://example.test", TOKEN, "edit-pc")
        ControlPlaneClient(
            "http://example.test", TOKEN, "edit-pc", allow_insecure_http=True
        )

    def test_picker_runs_on_worker_and_reports_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            client = FakeClient()
            worker = RemoteWorker(client, root, "python.exe", name="Editing PC")
            with mock.patch("src.remote_worker.native_picker", return_value=["F:/clip.mp4"]):
                worker.process_job(
                    {"job_id": "picker-1", "kind": "picker", "payload": {"kind": "videos"}}
                )
            self.assertEqual(client.reports[-1][1]["state"], "succeeded")
            self.assertEqual(client.reports[-1][1]["result"]["paths"], ["F:/clip.mp4"])

    def test_preview_fallback_never_selects_raw_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            rendered = root / "data" / "final.mp4"
            rendered.parent.mkdir()
            rendered.write_bytes(b"small rendered output")
            worker = RemoteWorker(FakeClient(), root, "python.exe", name="Editing PC")
            worker.manager.output_files = mock.Mock(
                return_value=[{"name": rendered.name, "path": str(rendered)}]
            )
            with mock.patch("src.remote_worker.shutil.which", return_value=None):
                self.assertEqual(worker._prepare_preview(), rendered.resolve())

    def test_real_outbound_protocol_registers_claims_reports_and_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "web"
            static.mkdir()
            (static / "index.html").write_text("ok", encoding="utf-8")
            with ControlPlaneStore(root / "state.sqlite3", root / "storage") as store:
                server = ControlPlaneServer(
                    ("127.0.0.1", 0), ControlPlaneHandler, store, static,
                    "admin-token-123456789", TOKEN, 1024 * 1024,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                client = ControlPlaneClient(
                    f"http://127.0.0.1:{server.server_port}", TOKEN, "edit-pc"
                )
                try:
                    head = urllib_request.Request(client.server_url + "/", method="HEAD")
                    with urllib_request.urlopen(head, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"")
                    client.register("Editing PC", {"hardware": {"gpu": "local-gpu"}})
                    created = store.create_job("edit-pc", "workflow", {"flow": "resolve"})
                    claimed = client.next_job()
                    self.assertEqual(claimed["job_id"], created["job_id"])
                    self.assertEqual(claimed["payload"]["flow"], "resolve")
                    self.assertFalse(client.report(
                        created["job_id"],
                        {"state": "running", "stage": "resolve", "progress": 80},
                    ))
                    preview = root / "preview.mp4"
                    preview.write_bytes(b"browser preview")
                    uploaded = client.upload_artifact(created["job_id"], preview)
                    self.assertEqual(uploaded["artifact"]["name"], "preview.mp4")
                    self.assertEqual(len(store.list_artifacts()), 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
