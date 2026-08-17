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

from src.frame_edl import build_frame_edl
from main import (
    build_parser,
    WindowsSleepInhibitor,
    WorkflowError,
    WorkflowLock,
    WorkflowOrchestrator,
)


class OrchestratorTests(unittest.TestCase):
    """Exercise orchestration without launching heavy dependencies."""

    def test_default_ollama_timeout_allows_slow_mixed_memory_directing(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.ollama_timeout, 7200)
        self.assertEqual(args.sample_interval, 0.5)
        self.assertEqual(args.max_keyframes, 14400)
        self.assertEqual(
            args.ollama_model,
            "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M",
        )
        self.assertEqual(
            args.director_model,
            "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0",
        )

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
            with self.assertRaisesRegex(WorkflowError, "stage_output.log"):
                orchestrator._run_stage(
                    "unit",
                    [sys.executable, "-c", "raise SystemExit(7)"],
                )

    def test_stage_persists_child_output_for_postmortem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = WorkflowOrchestrator(
                project_root=root,
                data_dir=root / "data",
                logger=logging.getLogger("test.orchestrator.output"),
            )
            orchestrator._run_stage(
                "diagnostic",
                [sys.executable, "-c", "print('child diagnostic sentinel')"],
            )

            transcript = (root / "data" / "stage_output.log").read_text(
                encoding="utf-8"
            )
            self.assertIn("diagnostic", transcript)
            self.assertIn("child diagnostic sentinel", transcript)

    def test_final_qa_false_cannot_be_reported_as_workflow_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "final_output_qa.json"
            report.write_text(
                json.dumps({"passes": False, "failures": ["duration mismatch"]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(WorkflowError, "duration mismatch"):
                WorkflowOrchestrator._require_passing_qa(report)

    def test_final_qa_requires_explicit_boolean_true(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "final_output_qa.json"
            report.write_text(json.dumps({"passes": "true"}), encoding="utf-8")

            with self.assertRaises(WorkflowError):
                WorkflowOrchestrator._require_passing_qa(report)

            report.write_text(json.dumps({"passes": True}), encoding="utf-8")
            WorkflowOrchestrator._require_passing_qa(report)

    def test_render_without_preview_still_requires_a_final_media_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "raw_data.json").write_text("{}", encoding="utf-8")
            (data_dir / "timeline_cuts.json").write_text(
                json.dumps({"project_fps": 25, "clips": [{"clip_id": 1}]}),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "--skip-extraction",
                    "--skip-director",
                    "--skip-preview",
                    "--render-final",
                    "--render-dir",
                    str(root / "final"),
                ]
            )
            orchestrator = WorkflowOrchestrator(
                project_root=root,
                data_dir=data_dir,
                logger=logging.getLogger("test.orchestrator.final-artifact"),
            )

            def fake_stage(_display_name, command):
                if "src.program_audio" in command:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"wav")

            with mock.patch.object(
                orchestrator, "_run_stage", side_effect=fake_stage
            ):
                with self.assertRaisesRegex(
                    WorkflowError, "no new or updated final media file"
                ):
                    orchestrator.run(args)

    def test_music_analysis_is_forced_to_cpu_and_frame_edl_precedes_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            library = root / "music"
            data.mkdir()
            library.mkdir()
            source = root / "source.mp4"
            source.write_bytes(b"video")
            (data / "raw_data.json").write_text("{}", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "--skip-extraction",
                    "--skip-preview",
                    "--skip-resolve",
                    "--project-fps",
                    "25",
                    "--music-provider",
                    "local",
                    "--music-folder",
                    str(library),
                ]
            )
            orchestrator = WorkflowOrchestrator(root, data)
            commands = []

            def fake_stage(_name, command):
                commands.append(list(command))
                module = command[command.index("-m") + 1]
                if module == "src.director" and "--treatment-only" in command:
                    Path(command[command.index("--treatment-output") + 1]).write_text(
                        "{}", encoding="utf-8"
                    )
                    Path(command[command.index("--music-brief-output") + 1]).write_text(
                        "{}", encoding="utf-8"
                    )
                elif module == "src.music_analyzer":
                    Path(command[command.index("--output") + 1]).write_text(
                        "{}", encoding="utf-8"
                    )
                elif module == "src.director":
                    Path(command[command.index("--output") + 1]).write_text(
                        json.dumps({
                            "project_fps": 25,
                            "clips": [{
                                "clip_id": 1, "file_name": str(source),
                                "cut_in_sec": 0, "cut_out_sec": 1,
                            }],
                        }),
                        encoding="utf-8",
                    )
                elif module == "src.frame_edl":
                    timeline = Path(command[command.index("--timeline") + 1])
                    payload = json.loads(timeline.read_text(encoding="utf-8"))
                    payload["frame_edl"] = build_frame_edl(
                        payload, source_fps_overrides=[25]
                    )
                    timeline.write_text(json.dumps(payload), encoding="utf-8")
                elif module in {"src.music_bed", "src.program_audio"}:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"wav")

            with (
                mock.patch.object(orchestrator, "_has_continuous_visual_review", return_value=True),
                mock.patch("main.ensure_ollama_service", return_value=([], False)),
                mock.patch.object(orchestrator, "_force_ollama_unload"),
                mock.patch.object(orchestrator, "_run_stage", side_effect=fake_stage),
            ):
                orchestrator.run(args)

            analyzer = next(command for command in commands if "src.music_analyzer" in command)
            device_index = analyzer.index("--vocal-audit-device")
            self.assertEqual(analyzer[device_index + 1], "cpu")
            modules = [command[command.index("-m") + 1] for command in commands]
            self.assertLess(modules.index("src.frame_edl"), modules.index("src.music_bed"))
            self.assertLess(modules.index("src.frame_edl"), modules.index("src.program_audio"))

    def test_failed_blind_review_never_starts_resolve(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            source = root / "source.mp4"
            source.write_bytes(b"video")
            (data / "raw_data.json").write_text("{}", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "--skip-extraction",
                    "--project-fps",
                    "25",
                    "--preview-review-rounds",
                    "0",
                ]
            )
            orchestrator = WorkflowOrchestrator(root, data)
            commands = []

            def fake_stage(_name, command):
                commands.append(list(command))
                module = command[command.index("-m") + 1]
                if module == "src.director" and "--treatment-only" in command:
                    Path(command[command.index("--treatment-output") + 1]).write_text(
                        "{}", encoding="utf-8"
                    )
                    Path(command[command.index("--music-brief-output") + 1]).write_text(
                        "{}", encoding="utf-8"
                    )
                elif module == "src.director":
                    Path(command[command.index("--output") + 1]).write_text(
                        json.dumps({
                            "project_fps": 25,
                            "clips": [{
                                "clip_id": 1, "file_name": str(source),
                                "cut_in_sec": 0, "cut_out_sec": 1,
                            }],
                        }),
                        encoding="utf-8",
                    )
                elif module == "src.frame_edl":
                    timeline = Path(command[command.index("--timeline") + 1])
                    payload = json.loads(timeline.read_text(encoding="utf-8"))
                    payload["frame_edl"] = build_frame_edl(
                        payload, source_fps_overrides=[25]
                    )
                    timeline.write_text(json.dumps(payload), encoding="utf-8")
                elif module == "src.program_audio":
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"wav")
                elif module == "src.review_renderer":
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"preview")
                elif module == "src.rough_cut_reviewer":
                    Path(command[command.index("--output") + 1]).write_text(
                        json.dumps({
                            "passes": False,
                            "blind_review": {"reason": "no coherent premise"},
                        }),
                        encoding="utf-8",
                    )

            with (
                mock.patch.object(orchestrator, "_has_continuous_visual_review", return_value=True),
                mock.patch("main.ensure_ollama_service", return_value=([], False)),
                mock.patch.object(orchestrator, "_force_ollama_unload"),
                mock.patch.object(orchestrator, "_run_stage", side_effect=fake_stage),
            ):
                with self.assertRaisesRegex(WorkflowError, "Resolve was not started"):
                    orchestrator.run(args)

            self.assertFalse(
                any("src.resolve_executor" in command for command in commands)
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

    def test_sleep_inhibitor_restores_state_after_failure(self):
        setter = mock.Mock(side_effect=[1, 1])
        guard = WindowsSleepInhibitor(logging.getLogger("test.sleep"))

        with (
            mock.patch.object(guard, "_is_supported", return_value=True),
            mock.patch.object(guard, "_set_execution_state", setter),
        ):
            with self.assertRaisesRegex(RuntimeError, "stage failed"):
                with guard:
                    raise RuntimeError("stage failed")

        expected_active = (
            WindowsSleepInhibitor.ES_CONTINUOUS
            | WindowsSleepInhibitor.ES_SYSTEM_REQUIRED
            | WindowsSleepInhibitor.ES_DISPLAY_REQUIRED
        )
        self.assertEqual(
            setter.call_args_list,
            [mock.call(expected_active), mock.call(WindowsSleepInhibitor.ES_CONTINUOUS)],
        )
        self.assertFalse(guard.active)

    def test_sleep_inhibitor_is_noop_on_unsupported_platform(self):
        setter = mock.Mock()
        guard = WindowsSleepInhibitor(logging.getLogger("test.sleep"))

        with (
            mock.patch.object(guard, "_is_supported", return_value=False),
            mock.patch.object(guard, "_set_execution_state", setter),
            guard,
        ):
            pass

        setter.assert_not_called()

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

    def test_continuous_visual_review_rejects_old_one_fps_metadata(self):
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

            self.assertFalse(
                WorkflowOrchestrator._has_continuous_visual_review(raw_data)
            )

    def test_continuous_visual_review_accepts_full_span_two_fps_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw_data = Path(temporary) / "raw_data.json"
            raw_data.write_text(
                json.dumps({
                    "assets": [{
                        "visual_sampling": {
                            "mode": "continuous_temporal_coverage",
                            "requested_interval_sec": 0.5,
                            "effective_min_gap_sec": 0.5,
                            "hard_cap": 14400,
                            "saved_frame_count": 2,
                            "complete_source_span": True,
                        },
                        "keyframes": [
                            {"timestamp_sec": 0},
                            {"timestamp_sec": 0.5},
                        ],
                    }],
                }),
                encoding="utf-8",
            )

            self.assertTrue(
                WorkflowOrchestrator._has_continuous_visual_review(raw_data)
            )

    def test_legacy_truncated_candidate_audit_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = root / "timeline_cuts.json"
            raw_data = root / "raw_data.json"
            raw_data.write_text("{}", encoding="utf-8")
            timeline.write_text(
                json.dumps({
                    "visual_review": {"mode": "continuous_all_saved_samples"},
                    "candidate_audit": [{"candidate_id": "C0001"}],
                }),
                encoding="utf-8",
            )

            self.assertFalse(
                WorkflowOrchestrator._has_reusable_candidate_audit(
                    timeline, raw_data, "vision:test"
                )
            )

    def test_versioned_complete_candidate_audit_can_be_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            from src.director import build_evidence_fingerprint

            root = Path(temporary)
            timeline = root / "timeline_cuts.json"
            raw_data = root / "raw_data.json"
            source = root / "source.mp4"
            proxy = root / "proxy.mp4"
            frame = root / "frame.jpg"
            source.write_bytes(b"source")
            proxy.write_bytes(b"proxy")
            frame.write_bytes(b"jpeg")
            raw_data.write_text(
                json.dumps({
                    "assets": [{
                        "asset_id": "asset-1",
                        "source_video": str(source),
                        "proxy_file_name": str(proxy),
                        "keyframes": [{"image_path": str(frame)}],
                    }],
                    "sampling": "two-fps",
                }),
                encoding="utf-8",
            )
            fingerprint = build_evidence_fingerprint(raw_data, "vision:test")
            timeline.write_text(
                json.dumps({
                    "visual_review": {
                        "mode": "neutral_complete_temporal_coverage",
                        "candidate_audit_complete": True,
                        "candidate_audit_version": 3,
                        "evidence_fingerprint": fingerprint,
                    },
                    "candidate_audit": [{"candidate_id": "C0001"}],
                }),
                encoding="utf-8",
            )

            self.assertTrue(
                WorkflowOrchestrator._has_reusable_candidate_audit(
                    timeline, raw_data, "vision:test"
                )
            )

            self.assertFalse(
                WorkflowOrchestrator._has_reusable_candidate_audit(
                    timeline, raw_data, "different-vision:test"
                )
            )

            raw_data.write_text(
                json.dumps({"assets": [], "sampling": "changed"}),
                encoding="utf-8",
            )
            self.assertFalse(
                WorkflowOrchestrator._has_reusable_candidate_audit(
                    timeline, raw_data, "vision:test"
                )
            )

    def test_version_two_candidate_audit_is_always_invalidated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_data = root / "raw_data.json"
            timeline = root / "timeline_cuts.json"
            raw_data.write_text("{}", encoding="utf-8")
            timeline.write_text(
                json.dumps({
                    "visual_review": {
                        "mode": "continuous_all_saved_samples",
                        "candidate_audit_complete": True,
                        "candidate_audit_version": 2,
                        "evidence_fingerprint": "legacy",
                    },
                    "candidate_audit": [{"candidate_id": "C0001"}],
                }),
                encoding="utf-8",
            )

            self.assertFalse(
                WorkflowOrchestrator._has_reusable_candidate_audit(
                    timeline, raw_data, "vision:test"
                )
            )


if __name__ == "__main__":
    unittest.main()
