from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from photocheck.core import metadata


class ExifToolResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        metadata.clear_metadata_cache()

    def tearDown(self) -> None:
        metadata.clear_metadata_cache()

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
                mock.patch.object(metadata, "PROJECT_ROOT", root),
            ):
                self.assertEqual(metadata.resolve_exiftool(), bundled.resolve())

    def test_development_tools_directory_is_discovered_from_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundled = root / "tools" / "exiftool" / "exiftool.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"")

            env = {key: value for key, value in os.environ.items() if key != "PHOTOCHECK_EXIFTOOL"}
            with (
                mock.patch.object(metadata.sys, "executable", str(root / ".venv" / "Scripts" / "python.exe")),
                mock.patch.object(metadata.shutil, "which", return_value=None),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(metadata, "PROJECT_ROOT", root),
            ):
                self.assertEqual(metadata.resolve_exiftool(), bundled.resolve())

    def test_invalid_explicit_path_fails_loudly(self) -> None:
        with mock.patch.object(metadata, "resolve_exiftool", wraps=metadata.resolve_exiftool):
            with self.assertRaisesRegex(metadata.MetadataScanError, "explicit path"):
                metadata.resolve_exiftool("Z:\\missing\\exiftool.exe")

    def test_metadata_cache_avoids_repeating_exiftool_for_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "image.jpg"
            image.write_bytes(b"image")
            executable = root / "exiftool.exe"
            executable.write_bytes(b"tool")
            completed = mock.Mock(returncode=0, stdout=b'[{"EXIF:Model":"x"}]', stderr=b"")
            with (
                mock.patch.object(metadata, "resolve_exiftool", return_value=executable),
                mock.patch.object(metadata.subprocess, "run", return_value=completed) as run,
            ):
                first = metadata.read_metadata(image)
                second = metadata.read_metadata(image)
            self.assertEqual(first, {"EXIF:Model": "x"})
            self.assertEqual(second, first)
            self.assertEqual(run.call_count, 1)
