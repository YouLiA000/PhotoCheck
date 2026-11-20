"""仓库隐私红线：原始 OCR 与姓名/地址候选不得重新入库。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(shutil.which("git") is None, "环境无 git")
class RawOcrEvidenceStaysLocal(unittest.TestCase):
    def setUp(self):
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            self.skipTest("当前目录尚未初始化 Git")

    def test_cache_and_candidate_exports_are_not_tracked(self):
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        forbidden = [
            path
            for path in out
            if path.endswith(("dev_ocr_cache.json", "real_ocr_cache.json", "candidates.csv"))
            and "/detectors/a_engine/reports/" in f"/{path}"
            and (ROOT / path).exists()
        ]
        self.assertEqual(
            forbidden,
            [],
            "Raw OCR/candidate exports must stay local: " + str(forbidden),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
