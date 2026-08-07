"""Tests for the deployable control plane. / 部署控制平面测试。"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

from src.control_plane import ControlPlaneStore


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


if __name__ == "__main__":
    unittest.main()
