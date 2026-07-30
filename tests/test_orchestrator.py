"""Tests for process serialization and workflow locking."""

import argparse
import logging
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
