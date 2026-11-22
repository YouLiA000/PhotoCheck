"""ExifTool is a hard dependency and self-check must fail closed."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photocheck.core.metadata import MetadataScanError  # noqa: E402
from tools import check_env  # noqa: E402


class ExifToolRequirementTests(unittest.TestCase):
    def test_missing_exiftool_sets_failure_exit_state(self) -> None:
        stream = io.StringIO()
        with (
            mock.patch.object(check_env, "resolve_exiftool", side_effect=MetadataScanError("missing")),
            mock.patch.object(check_env.shutil, "which", return_value=None),
            mock.patch.object(check_env, "ok", True),
            contextlib.redirect_stdout(stream),
        ):
            check_env.check_binaries()
            self.assertFalse(check_env.ok)
        self.assertIn("ExifTool 是硬依赖", stream.getvalue())

    def test_wrong_exiftool_version_sets_failure_exit_state(self) -> None:
        stream = io.StringIO()
        completed = mock.Mock(returncode=0, stdout="13.60\n")
        with (
            mock.patch.object(check_env, "resolve_exiftool", return_value=ROOT / "tools" / "exiftool" / "exiftool.exe"),
            mock.patch.object(check_env.shutil, "which", return_value=None),
            mock.patch.object(check_env.subprocess, "run", return_value=completed),
            mock.patch.object(check_env, "ok", True),
            contextlib.redirect_stdout(stream),
        ):
            check_env.check_binaries()
            self.assertFalse(check_env.ok)
        self.assertIn("期望 13.61", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
