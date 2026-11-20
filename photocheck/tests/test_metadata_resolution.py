from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from photocheck.core import metadata


class ExifToolResolutionTests(unittest.TestCase):
    def test_bundled_runtime_is_used_when_path_and_environment_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_python = root / "runtime" / "python.exe"
            runtime_python.parent.mkdir()
            runtime_python.write_bytes(b"")
            bundled = root / "runtime" / "exiftool" / "exiftool.exe"
            bundled.parent.mkdir()
            bundled.write_bytes(b"")

            env = {key: value for key, value in os.environ.items() if key != "PHOTOCHECK_EXIFTOOL"}
            with (
                mock.patch.object(metadata.sys, "executable", str(runtime_python)),
                mock.patch.object(metadata.shutil, "which", return_value=None),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(metadata, "DEFAULT_EXIFTOOL_PATH", root / "missing" / "exiftool.exe"),
            ):
                self.assertEqual(metadata.resolve_exiftool(), bundled.resolve())

    def test_invalid_explicit_path_fails_loudly(self) -> None:
        with mock.patch.object(metadata, "resolve_exiftool", wraps=metadata.resolve_exiftool):
            with self.assertRaisesRegex(metadata.MetadataScanError, "explicit path"):
                metadata.resolve_exiftool("Z:\\missing\\exiftool.exe")
