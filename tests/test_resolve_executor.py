"""Mocked integration tests for the Resolve executor."""

import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src.resolve_executor import (
    ClipDecision,
    DaVinciExecutor,
    Decimal,
    ResolveExecutorError,
)


class FakeItem:
    def __init__(self, path):
        self.path = str(Path(path).resolve())

    def GetName(self):
        return Path(self.path).name

    def GetClipProperty(self):
        return {"Clip Name": self.GetName(), "File Path": self.path}


class FakeFolder:
    def __init__(self):
        self.clips = []

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return []


class FakeTimeline:
    def __init__(self, name, fps):
        self.name = name
        self.fps = fps

    def GetName(self):
        return self.name

    def GetSetting(self, key):
        return self.fps


class FakeMediaPool:
    def __init__(self, project):
        self.project = project
        self.folder = FakeFolder()
        self.appended = []

    def GetRootFolder(self):
        return self.folder

    def CreateEmptyTimeline(self, name):
        self.project.timeline = FakeTimeline(name, self.project.fps)
        return self.project.timeline

    def ImportMedia(self, paths):
        items = [FakeItem(path) for path in paths]
        self.folder.clips.extend(items)
        return items

    def AppendToTimeline(self, clip_infos):
        self.appended.extend(clip_infos)
        return [{"timeline_item": len(self.appended)}]


class FakeProject:
    def __init__(self, fps="25"):
        self.name = "Mock Project"
        self.fps = fps
        self.timeline = None
        self.media_pool = FakeMediaPool(self)

    def GetName(self):
        return self.name

    def GetMediaPool(self):
        return self.media_pool

    def GetCurrentTimeline(self):
        return self.timeline

    def GetTimelineCount(self):
        return 0

    def SetCurrentTimeline(self, timeline):
        self.timeline = timeline
        return True

    def GetSetting(self, key):
        return self.fps

    def SetSetting(self, key, value):
        self.fps = value
        return True


class FakeManager:
    def __init__(self, project):
        self.project = project
        self.saved = False

    def GetCurrentProject(self):
        return self.project

    def SaveProject(self):
        self.saved = True
        return True


class FakeResolve:
    def __init__(self, project):
        self.manager = FakeManager(project)

    def GetProjectManager(self):
        return self.manager


class MockExecutor(DaVinciExecutor):
    def __init__(self, resolve, *args, **kwargs):
        self.fake_resolve = resolve
        super().__init__(*args, **kwargs)

    def connect(self):
        return self.fake_resolve


class ResolveExecutorTests(unittest.TestCase):
    """Validate frame math and a fully mocked Resolve run."""

    def test_seconds_to_inclusive_frames(self):
        self.assertEqual(
            DaVinciExecutor.seconds_to_frames(
                Decimal("12.5"), Decimal("18.2"), Decimal("25")
            ),
            (312, 454),
        )

    def test_native_source_fps_is_used_when_available(self):
        class NativeFpsItem:
            def GetClipProperty(self, name=None):
                properties = {"FPS": "50", "Frame Rate": "50"}
                return properties if name is None else properties.get(name, "")

        executor = DaVinciExecutor("timeline_cuts.json")
        self.assertEqual(executor._media_fps(NativeFpsItem()), Decimal("50"))

    def test_transient_untitled_project_is_replaced_with_named_project(self):
        transient = FakeProject()
        transient.name = "Untitled Project"

        class Manager:
            def __init__(self):
                self.current = transient

            def GetCurrentProject(self):
                return self.current

            def GetProjectListInCurrentFolder(self):
                return []

            def CreateProject(self, name):
                self.current = FakeProject()
                self.current.name = name
                return self.current

        manager = Manager()

        class Resolve:
            def GetProjectManager(self):
                return manager

            def GetCurrentPage(self):
                return None

        executor = DaVinciExecutor(
            "timeline_cuts.json", project_name="CyberEditor Project"
        )
        executor.resolve = Resolve()

        returned_manager, project = executor.ensure_project()

        self.assertIs(returned_manager, manager)
        self.assertEqual(project.GetName(), "CyberEditor Project")
        self.assertTrue(executor.created_project)

    def test_ntsc_fps_rounding_is_accepted_in_strict_mode(self):
        executor = DaVinciExecutor(
            "timeline_cuts.json", strict_fps=True
        )
        executor.compare_fps(Decimal("59.94006"), Decimal("59.94"))
        with self.assertRaises(ResolveExecutorError):
            executor.compare_fps(Decimal("60"), Decimal("59.94"))

    def test_new_project_fps_uses_resolve_ntsc_decimal(self):
        project = FakeProject()
        executor = DaVinciExecutor("timeline_cuts.json")
        executor.project = project

        executor.initialize_new_project_fps(Decimal("59.94006"))

        self.assertEqual(project.fps, "59.94")

    def test_sony_pp8_color_management_is_required_before_import(self):
        class ColorProject(FakeProject):
            def __init__(self):
                super().__init__()
                self.settings = {}

            def SetSetting(self, key, value):
                self.settings[key] = value
                return True

        project = ColorProject()
        executor = DaVinciExecutor("timeline_cuts.json")
        executor.project = project
        executor.color_pipeline = {
            "enabled": True,
            "camera_profile": "sony_pp8_slog3_sgamut3cine",
        }

        executor.configure_color_pipeline()

        self.assertEqual(project.settings["colorSpaceInput"], "Sony S-Gamut3.Cine")
        self.assertEqual(project.settings["colorSpaceInputGamma"], "S-Log3")
        self.assertEqual(project.settings["colorSpaceOutputGamma"], "Gamma 2.4")

    def test_timeline_creation_falls_back_to_documented_overload(self):
        project = FakeProject()

        class FallbackMediaPool(FakeMediaPool):
            def CreateEmptyTimeline(self, name):
                return None

            def CreateTimelineFromClips(self, name, clips):
                self.project.timeline = FakeTimeline(name, self.project.fps)
                return self.project.timeline

        project.media_pool = FallbackMediaPool(project)
        executor = DaVinciExecutor("timeline_cuts.json")
        executor.project = project
        executor.media_pool = project.media_pool

        timeline = executor.ensure_timeline()

        self.assertEqual(timeline.GetName(), "CyberEditor Timeline")

    def test_existing_current_timeline_is_preserved_and_new_name_is_unique(self):
        project = FakeProject()
        project.timeline = FakeTimeline("CyberEditor Timeline", project.fps)
        executor = DaVinciExecutor("timeline_cuts.json")
        executor.project = project
        executor.media_pool = project.media_pool

        timeline = executor.ensure_timeline()

        self.assertEqual(timeline.GetName(), "CyberEditor Timeline (2)")
        self.assertIs(project.GetCurrentTimeline(), timeline)

    def test_per_source_slog2_input_transform_is_applied(self):
        class MediaItem:
            def __init__(self):
                self.calls = []
                self.value = "Project"

            def SetClipProperty(self, key, value):
                self.calls.append((key, value))
                if key == "Input Color Space" and value == "Sony S-Gamut":
                    self.value = "S-Gamut/S-Log"
                    return True
                return False

            def GetClipProperty(self, key=None):
                if key == "Input Color Space":
                    return self.value
                return {"Input Color Space": self.value}

        decision = ClipDecision(
            1,
            "source.mp4",
            Decimal("0"),
            Decimal("2"),
            "Sony XML test",
            source_color={
                "is_log": True,
                "transform_supported": True,
                "resolve_input_color_space": "Sony S-Gamut",
                "resolve_input_gamma": "S-Log2",
            },
        )
        item = MediaItem()
        executor = DaVinciExecutor("timeline_cuts.json")

        executor._configure_media_input_transform(item, decision)

        self.assertEqual(item.calls[0], ("Input Color Space", "Sony S-Gamut/S-Log2"))
        self.assertIn(("Input Color Space", "Sony S-Gamut"), item.calls)
        self.assertNotIn(("Input Gamma", "S-Log2"), item.calls)

    def test_per_source_slog3_accepts_resolve_21_normalized_readback(self):
        class MediaItem:
            def __init__(self):
                self.value = "Project"

            def SetClipProperty(self, key, value):
                if key == "Input Color Space" and value == "Sony S-Gamut3.Cine":
                    self.value = "S-Gamut3.Cine/S-Log3"
                    return True
                return False

            def GetClipProperty(self, key=None):
                return self.value if key else {"Input Color Space": self.value}

        decision = ClipDecision(
            2,
            "source.mp4",
            Decimal("0"),
            Decimal("2"),
            "Sony XML test",
            source_color={
                "is_log": True,
                "transform_supported": True,
                "resolve_input_color_space": "Sony S-Gamut3.Cine",
                "resolve_input_gamma": "S-Log3",
            },
        )
        executor = DaVinciExecutor("timeline_cuts.json")

        executor._configure_media_input_transform(MediaItem(), decision)

    def test_preconformed_music_bed_allows_bounded_frame_rounding_without_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            bed = Path(temporary) / "music_bed.wav"
            bed.write_bytes(b"wav")

            class Timeline:
                def __init__(self):
                    self.tracks = 1

                def GetTrackCount(self, track_type):
                    return self.tracks

                def AddTrack(self, track_type, subtype):
                    self.tracks += 1
                    return True

                def GetStartFrame(self):
                    return 0

            class MediaPool:
                def __init__(self):
                    self.appended = []

                def AppendToTimeline(self, clip_infos):
                    self.appended.extend(clip_infos)
                    return [object()]

            prepared = [
                (
                    ClipDecision(
                        index,
                        f"source-{index}.mp4",
                        Decimal("0"),
                        Decimal("0.02"),
                        "frame rounding",
                    ),
                    {},
                )
                for index in range(3)
            ]
            executor = DaVinciExecutor("timeline_cuts.json")
            executor.music_plan = {"bed_file": str(bed)}
            executor.timeline = Timeline()
            executor.media_pool = MediaPool()

            with (
                mock.patch.object(executor, "_import_media", return_value=[object()]),
                mock.patch.object(executor, "_probe_media_duration", return_value=0.06),
            ):
                executor.append_music_bed(Decimal("25"), prepared)

            self.assertEqual(len(executor.media_pool.appended), 1)
            self.assertEqual(executor.media_pool.appended[0]["startFrame"], 0)
            self.assertEqual(executor.media_pool.appended[0]["endFrame"], 1)

    def test_resolve_process_detection_ignores_windows_code_page(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=b'"Resolve.exe","1234","Console"\r\n\xd0',
        )
        with mock.patch(
            "src.resolve_executor.subprocess.run", return_value=completed
        ) as run:
            self.assertTrue(DaVinciExecutor._is_resolve_running())

        self.assertIs(run.call_args.kwargs["text"], False)

    def test_resolve_process_detection_handles_missing_stdout(self):
        completed = SimpleNamespace(returncode=0, stdout=None)
        with mock.patch(
            "src.resolve_executor.subprocess.run", return_value=completed
        ):
            self.assertFalse(DaVinciExecutor._is_resolve_running())

    def test_ai_effect_plan_calls_supported_resolve_apis(self):
        class TimelineItem:
            def __init__(self):
                self.voice = None
                self.properties = None
                self.cdl = None
                self.marker = None

            def SetVoiceIsolationState(self, value):
                self.voice = value
                return True

            def SetProperty(self, value):
                self.properties = value
                return True

            def SetCDL(self, value):
                self.cdl = value
                return True

            def AddMarker(self, *value):
                self.marker = value
                return True

        executor = DaVinciExecutor("timeline_cuts.json")
        item = TimelineItem()
        decision = ClipDecision(
            1,
            "source.mp4",
            Decimal("1"),
            Decimal("3"),
            "Strong opening",
            "cross_dissolve",
            Decimal("0.5"),
            "strong",
            "warm",
            "gentle_push_in",
        )

        executor.apply_clip_effects(item, decision)

        self.assertEqual(item.voice["amount"], 75.0)
        self.assertEqual(item.properties["ZoomX"], 1.04)
        self.assertEqual(item.cdl["NodeIndex"], "1")
        self.assertIn("cross_dissolve", item.marker[-1])

    def test_director_creative_grade_modifies_resolve_cdl(self):
        class TimelineItem:
            def __init__(self):
                self.cdl = None

            def SetCDL(self, value):
                self.cdl = value
                return True

        item = TimelineItem()
        decision = ClipDecision(
            1,
            "source.mp4",
            Decimal("0"),
            Decimal("3"),
            "Payoff",
            color_look="neutral",
            creative_grade={
                "palette": "warm_memory", "exposure_ev": 0.2,
                "contrast": 1.12, "saturation": 1.08, "warmth": 0.3,
            },
        )

        DaVinciExecutor("timeline_cuts.json").apply_clip_effects(item, decision)

        self.assertIsNotNone(item.cdl)
        red, _, blue = [float(value) for value in item.cdl["Slope"].split()]
        self.assertGreater(red, blue)
        self.assertAlmostEqual(float(item.cdl["Saturation"]), 1.02 * 1.08, places=4)
        self.assertLess(float(item.cdl["Power"].split()[0]), 1.0)

    def test_native_ai_apis_and_drx_are_applied(self):
        class Graph:
            def __init__(self):
                self.applied = None

            def ApplyGradeFromDRX(self, path, mode):
                self.applied = (path, mode)
                return True

        class TimelineItem:
            def __init__(self):
                self.graph = Graph()
                self.stabilized = False
                self.mask_mode = None
                self.reframed = False
                self.volume = None

            def GetNodeGraph(self):
                return self.graph

            def Stabilize(self):
                self.stabilized = True
                return True

            def CreateMagicMask(self, mode):
                self.mask_mode = mode
                return True

            def SmartReframe(self):
                self.reframed = True
                return True

            def GetProperty(self):
                return {"Audio Level": 0.0}

            def SetProperty(self, key, value):
                self.volume = (key, value)
                return True

        with tempfile.TemporaryDirectory() as temporary:
            drx_root = Path(temporary)
            preset = drx_root / "cinematic.drx"
            preset.write_bytes(b"mock drx")
            executor = DaVinciExecutor(
                "timeline_cuts.json", drx_root=drx_root
            )
            item = TimelineItem()
            decision = ClipDecision(
                1,
                "source.mp4",
                Decimal("1"),
                Decimal("3"),
                "Track a moving subject",
                volume_db=Decimal("-3.5"),
                drx_preset="cinematic",
                stabilization="auto",
                tracking="magic_mask_bidirectional",
                smart_reframe=True,
            )

            executor.apply_clip_effects(item, decision)

            self.assertEqual(item.volume, ("Audio Level", -3.5))
            self.assertEqual(item.graph.applied, (str(preset.resolve()), 0))
            self.assertTrue(item.stabilized)
            self.assertEqual(item.mask_mode, "BI")
            self.assertTrue(item.reframed)

    def test_final_render_uses_current_settings_and_waits_for_completion(self):
        class RenderProject:
            def __init__(self):
                self.settings = None
                self.started = None

            def SetRenderSettings(self, settings):
                self.settings = settings
                return True

            def AddRenderJob(self):
                return "job-1"

            def StartRendering(self, job_ids, interactive):
                self.started = (job_ids, interactive)
                return True

            def GetRenderJobStatus(self, job_id):
                return {"JobStatus": "Complete", "CompletionPercentage": 100}

            def IsRenderingInProgress(self):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "final"
            executor = DaVinciExecutor(
                "timeline_cuts.json",
                render_enabled=True,
                render_dir=output,
                render_name="documentary",
            )
            executor.project = RenderProject()

            status = executor.render_final()

            self.assertEqual(status["JobStatus"], "Complete")
            self.assertEqual(executor.project.started, (["job-1"], False))
            self.assertEqual(
                executor.project.settings["TargetDir"], str(output.resolve())
            )
            self.assertEqual(
                executor.project.settings["CustomName"], "documentary"
            )

    def test_final_render_accepts_localized_complete_status(self):
        class LocalizedRenderProject:
            def SetRenderSettings(self, settings):
                return True

            def AddRenderJob(self):
                return "job-localized"

            def StartRendering(self, job_ids, interactive):
                return True

            def GetRenderJobStatus(self, job_id):
                return {
                    "JobStatus": "完成",
                    "CompletionPercentage": 100,
                }

            def IsRenderingInProgress(self):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            executor = DaVinciExecutor(
                "timeline_cuts.json",
                render_enabled=True,
                render_dir=Path(temporary),
            )
            executor.project = LocalizedRenderProject()

            status = executor.render_final()

            self.assertEqual(status["JobStatus"], "完成")

    def test_mocked_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "proxy.mp4"
            media.write_bytes(b"mock")
            plan = root / "timeline_cuts.json"
            plan.write_text(
                json.dumps(
                    {
                        "project_fps": 25,
                        "clips": [
                            {
                                "clip_id": 1,
                                "file_name": "proxy.mp4",
                                "cut_in_sec": 1.0,
                                "cut_out_sec": 2.0,
                                "reason_for_cut": "test",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            project = FakeProject()
            resolve = FakeResolve(project)
            executor = MockExecutor(
                resolve,
                json_path=plan,
                media_root=root,
                logger=logging.getLogger("test.resolve"),
            )
            result = executor.run()
            self.assertEqual(len(result), 1)
            self.assertEqual(
                project.media_pool.appended[0]["startFrame"], 25
            )
            self.assertEqual(
                project.media_pool.appended[0]["endFrame"], 49
            )
            self.assertTrue(resolve.manager.saved)

    def test_custom_install_configures_fusionscript_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "DaVinci Resolve" / "Resolve.exe"
            library = executable.parent / "fusionscript.dll"
            executable.parent.mkdir(parents=True)
            executable.touch()
            library.touch()
            with mock.patch.dict(
                "os.environ", {"RESOLVE_SCRIPT_LIB": ""}, clear=False
            ):
                from src.resolve_executor import os

                os.environ.pop("RESOLVE_SCRIPT_LIB", None)
                DaVinciExecutor._configure_resolve_library(executable)
                self.assertEqual(
                    os.environ.get("RESOLVE_SCRIPT_LIB"), str(library)
                )

    def test_connect_auto_starts_and_waits_for_resolve(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "timeline_cuts.json"
            plan.write_text("{}", encoding="utf-8")
            executable = root / "Resolve.exe"
            executable.touch()
            expected = FakeResolve(FakeProject())

            module = mock.Mock()
            module.scriptapp.side_effect = [None, expected]
            launched = mock.Mock()
            launched.poll.return_value = None
            executor = DaVinciExecutor(
                json_path=plan,
                startup_timeout=5,
                logger=logging.getLogger("test.resolve.start"),
            )
            with (
                mock.patch(
                    "src.resolve_executor.platform.system",
                    return_value="Windows",
                ),
                mock.patch(
                    "src.resolve_executor.find_resolve_executable",
                    return_value=executable,
                ),
                mock.patch.object(
                    executor, "_configure_resolve_library"
                ),
                mock.patch.object(
                    executor,
                    "_load_resolve_module",
                    return_value=(module, [], []),
                ),
                mock.patch.object(
                    executor, "_is_resolve_running", return_value=False
                ),
                mock.patch.object(
                    executor, "_launch_resolve", return_value=launched
                ) as launch,
                mock.patch("src.resolve_executor.time.sleep"),
            ):
                result = executor.connect()

            self.assertIs(result, expected)
            launch.assert_called_once_with(executable)

    def test_preconformed_program_audio_makes_picture_video_only_and_uses_a1(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            program = root / "program_audio.wav"
            source.write_bytes(b"video")
            program.write_bytes(b"audio")
            executor = DaVinciExecutor(root / "timeline_cuts.json")
            executor.audio_program = {"bed_file": str(program)}

            class Item:
                def GetClipProperty(self, name=None):
                    values = {"FPS": "25", "File Path": str(source), "Clip Name": source.name}
                    return values if name is None else values.get(name, "")

            executor.media_pool = mock.Mock()
            executor.timeline = mock.Mock()
            executor.timeline.GetTrackCount.return_value = 1
            executor.timeline.GetStartFrame.return_value = 0
            executor.media_pool.AppendToTimeline.return_value = [object()]
            decision = ClipDecision(1, str(source), Decimal("0"), Decimal("2"), "test")
            with (
                mock.patch.object(executor, "_index_media_pool", return_value=[]),
                mock.patch.object(executor, "_resolve_media", return_value=(Item(), [])),
                mock.patch.object(executor, "_configure_media_input_transform"),
                mock.patch.object(executor, "_probe_media_duration", side_effect=[2.0, 2.0]),
                mock.patch.object(executor, "_import_media", return_value=[object()]),
            ):
                prepared = executor.prepare_clips([decision], Decimal("25"))
                appended = executor.append_program_audio(Decimal("25"), prepared)

            self.assertEqual(prepared[0][1]["mediaType"], 1)
            self.assertEqual(len(appended), 1)
            audio_info = executor.media_pool.AppendToTimeline.call_args.args[0][0]
            self.assertEqual(audio_info["mediaType"], 2)
            self.assertEqual(audio_info["trackIndex"], 1)
            self.assertEqual(audio_info["recordFrame"], 0)


if __name__ == "__main__":
    unittest.main()
