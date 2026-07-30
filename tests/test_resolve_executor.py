"""Mocked integration tests for the Resolve executor."""

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.resolve_executor import DaVinciExecutor, Decimal


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


if __name__ == "__main__":
    unittest.main()
