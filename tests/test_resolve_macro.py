"""Standard-library validation tests for the guarded macro profile."""

import json
from pathlib import Path
import tempfile
import unittest

from src.resolve_macro import ResolveMacroError, SafeResolveMacroRunner


class ResolveMacroTests(unittest.TestCase):
    def test_profile_is_loaded_without_importing_pyautogui(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "expected_resolution": [3840, 2160],
                        "actions": {
                            "post_assembly": [
                                {"type": "wait", "seconds": 0.1}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = SafeResolveMacroRunner(profile)
            self.assertIn("post_assembly", runner.profile["actions"])

    def test_invalid_resolution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "expected_resolution": [0, 2160],
                        "actions": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ResolveMacroError):
                SafeResolveMacroRunner(profile)


if __name__ == "__main__":
    unittest.main()
