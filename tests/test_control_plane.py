"""Tests for the deployable control plane. / 部署控制平面测试。"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import threading
import unittest
from urllib import error as urllib_error
from urllib import request as urllib_request

from src.control_plane import (
    ControlPlaneHandler,
    ControlPlaneServer,
    ControlPlaneStore,
    _accepts_html,
)


class ControlPlaneStoreTests(unittest.TestCase):
    """Validate queue isolation, log dedupe, cancellation, and artifacts.

    校验队列隔离、日志去重、取消以及预览文件边界。
    """

    def make_store(self, root: Path) -> ControlPlaneStore:
        """Create a temporary SQLite control plane. / 创建临时 SQLite 控制平面。"""
        return ControlPlaneStore(root / "state.sqlite3", root / "storage")

    def test_worker_job_lifecycle_and_incremental_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.make_store(Path(temporary)) as store:
                worker = store.register_worker(
                    "edit-pc", "Editing PC", {"hardware": {"gpu": "RTX"}}
                )
                self.assertTrue(worker["online"])
                created = store.create_job("edit-pc", "workflow", {"videos": ["F:/clip.mp4"]})
                claimed = store.claim_next("edit-pc")
                self.assertEqual(claimed["job_id"], created["job_id"])
                self.assertEqual(claimed["payload"]["videos"], ["F:/clip.mp4"])

                report = {
                    "state": "running", "stage": "extract", "progress": 20,
                    "logs": [{"id": 7, "timestamp": 1.0, "level": "info", "message": "extract"}],
                }
                self.assertFalse(store.report("edit-pc", created["job_id"], report)["cancel_requested"])
                store.report("edit-pc", created["job_id"], report)
                status = store.job_status(created["job_id"])
                self.assertEqual(len(status["logs"]), 1)
                self.assertEqual(status["logs"][0]["message"], "extract")

                store.report(
                    "edit-pc", created["job_id"],
                    {"state": "succeeded", "stage": "complete", "progress": 100, "return_code": 0},
                )
                self.assertFalse(store.job_status(created["job_id"])["running"])
                self.assertEqual(store.worker("edit-pc")["status"], "online")

    def test_only_one_workflow_can_run_per_worker_and_cancel_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.make_store(Path(temporary)) as store:
                store.register_worker("edit-pc", "Editing PC", {})
                job = store.create_job("edit-pc", "workflow", {})
                with self.assertRaisesRegex(RuntimeError, "active workflow"):
                    store.create_job("edit-pc", "workflow", {})
                store.claim_next("edit-pc")
                store.request_stop(job["job_id"])
                result = store.report(
                    "edit-pc", job["job_id"],
                    {"state": "stopping", "stage": "stopping", "progress": 50},
                )
                self.assertTrue(result["cancel_requested"])

    def test_invalid_worker_id_and_queued_cancellation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.make_store(Path(temporary)) as store:
                with self.assertRaisesRegex(ValueError, "Worker ID"):
                    store.register_worker("../other-host", "Bad", {})
                store.register_worker("edit-pc", "Editing PC", {})
                job = store.create_job("edit-pc", "workflow", {})
                status = store.request_stop(job["job_id"])
                self.assertEqual(status["state"], "stopped")
                self.assertIsNone(store.claim_next("edit-pc"))

    def test_picker_result_and_preview_are_scoped_to_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.make_store(root) as store:
                store.register_worker("edit-pc", "Editing PC", {})
                job = store.create_job("edit-pc", "picker", {"kind": "videos"})
                store.claim_next("edit-pc")
                store.report(
                    "edit-pc", job["job_id"],
                    {"state": "succeeded", "stage": "complete", "progress": 100,
                     "result": {"paths": ["F:/clip.mp4"]}},
                )
                self.assertEqual(store.wait_terminal(job["job_id"], 0.1)["result"]["paths"], ["F:/clip.mp4"])

                artifact = store.save_artifact(
                    "edit-pc", job["job_id"], "../preview.mp4", "video/mp4",
                    io.BytesIO(b"preview"), len(b"preview"),
                )
                path, mime = store.artifact(artifact["artifact_id"])
                self.assertEqual(path.read_bytes(), b"preview")
                self.assertEqual(path.name.endswith("preview.mp4"), True)
                self.assertEqual(mime, "video/mp4")
                path.relative_to((root / "storage").resolve())

    def test_preview_storage_prunes_oldest_files_before_capacity_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ControlPlaneStore(
                root / "state.sqlite3",
                root / "storage",
                max_storage_bytes=10,
                artifact_retention_seconds=3600,
            ) as store:
                store.register_worker("edit-pc", "Editing PC", {})
                job = store.create_job("edit-pc", "picker", {"kind": "videos"})
                first = store.save_artifact(
                    "edit-pc", job["job_id"], "first.mp4", "video/mp4",
                    io.BytesIO(b"123456"), 6,
                )
                second = store.save_artifact(
                    "edit-pc", job["job_id"], "second.mp4", "video/mp4",
                    io.BytesIO(b"abcdef"), 6,
                )
                with self.assertRaises(KeyError):
                    store.artifact(first["artifact_id"])
                self.assertEqual(store.artifact(second["artifact_id"])[0].read_bytes(), b"abcdef")


class ControlPlaneRoutingTests(unittest.TestCase):
    """Keep browser navigation separate from the JSON control-plane contract."""

    def test_browser_documents_receive_project_owned_not_found_page(self) -> None:
        self.assertTrue(_accepts_html("text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"))

    def test_api_and_asset_requests_do_not_receive_html(self) -> None:
        self.assertFalse(_accepts_html("application/json"))
        self.assertFalse(_accepts_html("text/css,*/*;q=0.1"))
        self.assertFalse(_accepts_html("*/*"))

    def test_unknown_browser_route_serves_project_page_with_404_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "web"
            static.mkdir()
            (static / "index.html").write_text("home", encoding="utf-8")
            (static / "not-found.html").write_text("CyberEditor route not found", encoding="utf-8")
            with ControlPlaneStore(root / "state.sqlite3", root / "storage") as store:
                server = ControlPlaneServer(
                    ("127.0.0.1", 0), ControlPlaneHandler, store, static,
                    "admin-token-123456789", "worker-token-123456789", 1024,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                request = urllib_request.Request(
                    f"http://127.0.0.1:{server.server_port}/missing",
                    headers={"Accept": "text/html"},
                )
                try:
                    with self.assertRaises(urllib_error.HTTPError) as raised:
                        urllib_request.urlopen(request, timeout=3)
                    response = raised.exception
                    try:
                        self.assertEqual(response.code, 404)
                        self.assertEqual(response.headers.get_content_type(), "text/html")
                        self.assertIn(b"CyberEditor route not found", response.read())
                    finally:
                        response.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
