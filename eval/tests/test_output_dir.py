"""`--out` 指向已存在目录时必须拒绝，否则一次误敲就覆盖基线。

默认路径本来就是原子分配 + 同秒加后缀，从不覆盖；但显式 `--out` 曾直通写盘，
这条测试锁住「显式路径也要过同一道关」。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.eval import resolve_output_dir  # noqa: E402


class ExplicitOut(unittest.TestCase):
    def test_empty_existing_dir_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            self.assertEqual(resolve_output_dir(str(out)), out)

    def test_non_empty_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            (out / "summary.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                resolve_output_dir(str(out))

    def test_force_allows_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            (out / "summary.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_output_dir(str(out), force=True), out)

    def test_missing_dir_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a" / "b"
            self.assertEqual(resolve_output_dir(str(out)), out)
            self.assertTrue(out.is_dir())


class DefaultOut(unittest.TestCase):
    def setUp(self):
        self.made = []

    def tearDown(self):
        import shutil
        for d in self.made:
            shutil.rmtree(d, ignore_errors=True)

    def test_two_calls_get_distinct_dirs(self):
        a = resolve_output_dir(None)
        b = resolve_output_dir(None)
        self.made += [a, b]
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_dir() and b.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
