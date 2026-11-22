"""判读册排版前的空图守卫。

R-04 的 `manual_crops` 裁自**已遮盖**图，裁剪范围就是遮盖范围，所以天然全黑。
若不拦，就会印出 5 页黑条——评审只能全勾 not_readable，判读在证据上是空的。

本测试锁三件事：默认拒绝、显式放行、有内容时正常生成。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import make_r04_booklet as booklet  # noqa: E402


def build_case(
    root: Path,
    *,
    blank: bool,
    count: int = 2,
    white: bool = False,
    control_blank: bool = False,
) -> Path:
    crops = root / "manual_crops"
    crops.mkdir(parents=True)
    controls = root / "control_crops"
    controls.mkdir(parents=True)
    rows: list[dict[str, str]] = []
    for i in range(1, count + 1):
        if blank:
            arr = np.zeros((40, 220), dtype=np.uint8)
        elif white:
            arr = np.full((40, 220), 255, dtype=np.uint8)
        else:
            arr = np.full((40, 220), 255, dtype=np.uint8)
            arr[10:30, 20:200] = 0  # 有字迹的痕迹
        Image.fromarray(arr).save(crops / f"{i:02d}.png")
        if control_blank:
            control_arr = np.zeros((40, 220), dtype=np.uint8)
        else:
            control_arr = np.full((40, 220), 255, dtype=np.uint8)
            control_arr[10:30, 20:200] = 0
        Image.fromarray(control_arr).save(controls / f"{i:02d}.png")
        rows.append({"sample_no": str(i), "subtype": "phone"})

    with (root / "r04-objects.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["sample_no", "subtype"])
        writer.writeheader()
        writer.writerows(rows)
    return root


class BlankCropGuard(unittest.TestCase):
    def test_blank_crops_are_refused_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=True, control_blank=True)
            code = booklet.main(["--objects", str(src)])
            self.assertEqual(code, 3, "全黑裁剪图应当被拒绝，而不是印成黑条")
            self.assertFalse((src / "print").exists(), "拒绝时不应产出任何 PDF")

    def test_allow_flag_permits_blank_for_leak_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=True, control_blank=True)
            code = booklet.main(["--objects", str(src), "--allow-blank-crops"])
            self.assertEqual(code, 0)
            self.assertTrue((src / "print" / "R04判读册_评审A.pdf").is_file())
            self.assertTrue((src / "print" / "R04判读册_评审B.pdf").is_file())

    def test_content_crops_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=False)
            code = booklet.main(["--objects", str(src)])
            self.assertEqual(code, 0)

    def test_black_after_with_content_control_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=True, control_blank=False)
            code = booklet.main(["--objects", str(src)])
            self.assertEqual(code, 0)

    def test_missing_crop_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=False, count=2)
            (src / "manual_crops" / "02.png").unlink()
            code = booklet.main(["--objects", str(src)])
            self.assertEqual(code, 2)

    def test_missing_control_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=False, count=2)
            (src / "control_crops" / "02.png").unlink()
            code = booklet.main(["--objects", str(src)])
            self.assertEqual(code, 2)

    def test_blank_ratio_reads_zero_when_crops_have_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=False)
            rows = [{"sample_no": "1"}, {"sample_no": "2"}]
            self.assertEqual(booklet.blank_ratio(src / "manual_crops", rows), 0.0)

    def test_blank_ratio_reads_one_for_pure_black(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=True)
            rows = [{"sample_no": "1"}, {"sample_no": "2"}]
            self.assertEqual(booklet.blank_ratio(src / "manual_crops", rows), 1.0)

    def test_blank_ratio_reads_one_for_pure_white(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = build_case(Path(tmp) / "src", blank=False, white=True)
            rows = [{"sample_no": "1"}, {"sample_no": "2"}]
            self.assertEqual(booklet.blank_ratio(src / "manual_crops", rows), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
