"""封存枚举必须**全量递归**，否则「封存后新增」永远检测不到。

原实现按 `images/*.jpg` + `labels/*.json` + 三个固定根文件的白名单收集：
白名单之外的文件（`*.png`、子目录杂项）根本进不了 `actual` 集合，
`verify` 的「封存后新增」分支就永远走不到——铁律 6 依赖的机制形同虚设。

本测试用合成的小 lock 目录（不碰真实 lock），锁住三件事：
新增的任意类型文件能被检出、子目录新增能被检出、重复记录不被静默覆盖。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataset.tools import seal_lock  # noqa: E402


def build_lock(root: Path) -> Path:
    lock = root / "lock"
    (lock / "images").mkdir(parents=True)
    (lock / "labels").mkdir()
    (lock / "images" / "image-lock-001.jpg").write_bytes(b"fake-jpeg")
    (lock / "labels" / "image-lock-001.json").write_text("{}", encoding="utf-8")
    (lock / "manifest.csv").write_text("file\n", encoding="utf-8")
    seal_lock.seal(lock)
    return lock


class FullTreeEnumeration(unittest.TestCase):
    def test_clean_lock_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = build_lock(Path(tmp))
            self.assertEqual(seal_lock.verify(lock), [])

    def test_extra_file_type_is_reported(self):
        # 白名单外的扩展名：旧实现完全看不见
        with tempfile.TemporaryDirectory() as tmp:
            lock = build_lock(Path(tmp))
            (lock / "stray.png").write_bytes(b"x")
            problems = seal_lock.verify(lock)
            self.assertTrue(any("stray.png" in p for p in problems), problems)

    def test_nested_directory_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = build_lock(Path(tmp))
            (lock / "notes").mkdir()
            (lock / "notes" / "leak.txt").write_text("x", encoding="utf-8")
            problems = seal_lock.verify(lock)
            self.assertTrue(any("notes/leak.txt" in p for p in problems), problems)

    def test_deleted_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = build_lock(Path(tmp))
            (lock / "labels" / "image-lock-001.json").unlink()
            problems = seal_lock.verify(lock)
            self.assertTrue(any("文件缺失" in p for p in problems), problems)


class DuplicateRecord(unittest.TestCase):
    def test_duplicate_line_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = build_lock(Path(tmp))
            sums = lock / seal_lock.SUMS_NAME
            lines = sums.read_text(encoding="utf-8").splitlines()
            # 追加一条与首行同路径、不同哈希的记录：旧实现会静默覆盖
            first = lines[0]
            fake = "0" * 64 + "  " + first.split("  ", 1)[1]
            sums.write_text("\n".join(lines + [fake]) + "\n", encoding="utf-8", newline="\n")
            problems = seal_lock.verify(lock)
            self.assertTrue(any("重复记录" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
