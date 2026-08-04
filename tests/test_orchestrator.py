"""Tests for process serialization and workflow locking."""

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from main import WorkflowError, WorkflowLock, WorkflowOrchestrator


class OrchestratorTests(unittest.TestCase):
    """Exercise orchestration without launching heavy dependencies."""

    def test_stage_waits_for_child_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = WorkflowOrchestrator(
                project_root=root,
                data_dir=root / "data",
                logger=logging.getLogger("test.orchestrator"),
            )
            orchestrator._run_stage(
                "unit",
                [sys.executable, "-c", "raise SystemExit(0)"],
            )
            self.assertIsNone(orchestrator.active_process)

    def test_stage_propagates_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = WorkflowOrchestrator(
                project_root=root,
                data_dir=root / "data",
                logger=logging.getLogger("test.orchestrator"),
            )
            with self.assertRaises(WorkflowError):
                orchestrator._run_stage(
                    "unit",
                    [sys.executable, "-c", "raise SystemExit(7)"],
                )

    def test_lock_is_exclusive_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "workflow.lock"
            with WorkflowLock(lock_path):
                self.assertTrue(lock_path.exists())
                with self.assertRaises(WorkflowError):
                    with WorkflowLock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())

    def test_stale_lock_is_recovered_automatically(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "workflow.lock"
            lock_path.write_text("2147483647", encoding="ascii")

            with WorkflowLock(lock_path):
                self.assertEqual(
                    lock_path.read_text(encoding="ascii"), str(os.getpid())
                )

            self.assertFalse(lock_path.exists())

    def test_text_only_model_is_rejected_before_extraction(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"capabilities":["completion"]}'
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = WorkflowOrchestrator(root, root / "data")
            with (
                mock.patch("main.ensure_ollama_service", return_value=([], False)),
                mock.patch("main.urllib_request.urlopen", return_value=response),
            ):
                with self.assertRaisesRegex(WorkflowError, "vision model"):
                    orchestrator._require_vision_model(
                        "qwen2.5:3b", "http://localhost:11434"
                    )

    def test_all_skipped_uses_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "raw_data.json").write_text("{}", encoding="utf-8")
            (data / "timeline_cuts.json").write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                video=None,
                proxy=None,
                skip_extraction=True,
                skip_director=True,
                skip_resolve=True,
                whisper_model="tiny",
                whisper_device="cpu",
                scene_threshold=0.2,
                sample_interval=2,
                max_keyframes=10,
                language=None,
                ollama_model="test",
                ollama_url="http://localhost:11434",
                chunk_minutes=10,
                project_fps=25,
                num_ctx=4096,
                ollama_timeout=10,
                media_root=None,
                timeline_name="test",
                project_name="test",
                strict_fps=False,
                log_level="INFO",
            )
            orchestrator = WorkflowOrchestrator(
                project_root=root,
                data_dir=data,
                logger=logging.getLogger("test.orchestrator"),
            )
            orchestrator.run(args)

    def test_continuous_visual_review_rejects_legacy_sparse_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw_data = Path(temporary) / "raw_data.json"
            raw_data.write_text(
                json.dumps({"assets": [{"keyframes": [{"timestamp_sec": 0}]}]}),
                encoding="utf-8",
            )

            self.assertFalse(
                WorkflowOrchestrator._has_continuous_visual_review(raw_data)
            )

    def test_continuous_visual_review_accepts_full_span_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw_data = Path(temporary) / "raw_data.json"
            raw_data.write_text(
                json.dumps({
                    "assets": [{
                        "visual_sampling": {
                            "mode": "continuous_temporal_coverage",
                            "requested_interval_sec": 1.0,
                            "saved_frame_count": 2,
                            "complete_source_span": True,
                        },
                        "keyframes": [
                            {"timestamp_sec": 0},
                            {"timestamp_sec": 1},
                        ],
                    }],
                }),
                encoding="utf-8",
            )

            self.assertTrue(
                WorkflowOrchestrator._has_continuous_visual_review(raw_data)
            )


if __name__ == "__main__":
    unittest.main()
