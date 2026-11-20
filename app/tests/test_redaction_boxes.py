"""遮盖框的准备逻辑（合并被拆开的框 + 外扩）。

2026-11-08 的 bug：引擎把一个号码读成两个框（`139-1234-` 与 `5678`），
逐个遮盖时中间留了一条缝，导出图上能直接看到 `-56` 露在外面。
这几条测试就是钉住那条缝。
"""

from __future__ import annotations

import unittest

from app import service


def phone(box, mask, det_id="d1"):
    return {
        "id": det_id, "kind": "text", "subtype": "phone", "bbox": list(box),
        "risk": 2, "details": {"masked_preview": mask},
    }


class MergeDuplicateBoxes(unittest.TestCase):
    def setUp(self) -> None:
        self.w, self.h = 1000, 800

    def test_same_value_split_in_two_is_merged(self):
        """同一个掩码、同一行、中间有缝 —— 必须合成一个框，否则缝里露数字。"""
        left = phone([100, 300, 200, 40], "139****5678", "d1")
        right = phone([380, 300, 120, 40], "139****5678", "d2")
        out = service._prepare_redaction_boxes([left, right], self.w, self.h)
        self.assertEqual(out[0]["bbox"], out[1]["bbox"], "两个框应统一成同一个合并框")
        box = out[0]["bbox"]
        self.assertLessEqual(box[0], 100, "合并框必须覆盖左框")
        self.assertGreaterEqual(box[0] + box[2], 500, "合并框必须覆盖右框")

    def test_different_values_on_same_line_are_kept_apart(self):
        """同一行上两个不同的号码，间距够大就不能粘在一起。"""
        left = phone([100, 300, 200, 40], "138****1111", "d1")
        right = phone([600, 300, 200, 40], "139****2222", "d2")
        out = service._prepare_redaction_boxes([left, right], self.w, self.h)
        self.assertNotEqual(out[0]["bbox"], out[1]["bbox"])

    def test_adjacent_fragments_are_merged_even_without_same_mask(self):
        """被切开的 token：掩码不同但紧挨着，也要合并。"""
        left = phone([100, 300, 150, 40], "139****", "d1")
        right = phone([262, 300, 80, 40], "5678", "d2")
        out = service._prepare_redaction_boxes([left, right], self.w, self.h)
        self.assertEqual(out[0]["bbox"], out[1]["bbox"])

    def test_different_rows_are_not_merged(self):
        """上下两行的号码不能合并成一个大黑块（那会把中间那行也盖掉）。"""
        top = phone([100, 300, 200, 40], "138****1111", "d1")
        bottom = phone([100, 500, 200, 40], "139****2222", "d2")
        out = service._prepare_redaction_boxes([top, bottom], self.w, self.h)
        self.assertNotEqual(out[0]["bbox"], out[1]["bbox"])

    def test_different_subtype_not_merged(self):
        idcard = {
            "id": "d2", "kind": "text", "subtype": "idcard", "bbox": [380, 300, 120, 40],
            "risk": 2, "details": {"masked_preview": "139****5678"},
        }
        out = service._prepare_redaction_boxes(
            [phone([100, 300, 200, 40], "139****5678"), idcard], self.w, self.h
        )
        self.assertNotEqual(out[0]["bbox"], out[1]["bbox"])

    def test_box_is_padded(self):
        """单框也要外扩，盖住边缘的半个笔画。"""
        out = service._prepare_redaction_boxes(
            [phone([100, 300, 200, 40], "139****5678")], self.w, self.h
        )
        box = out[0]["bbox"]
        self.assertLess(box[0], 100)
        self.assertLess(box[1], 300)
        self.assertGreater(box[0] + box[2], 300)

    def test_padding_is_clamped_to_image(self):
        """贴边的框外扩后不能越界，否则构造计划会失败。"""
        out = service._prepare_redaction_boxes(
            [phone([0, 0, 200, 40], "139****5678")], self.w, self.h
        )
        box = out[0]["bbox"]
        self.assertGreaterEqual(box[0], 0)
        self.assertGreaterEqual(box[1], 0)
        self.assertLessEqual(box[0] + box[2], self.w)
        self.assertLessEqual(box[1] + box[3], self.h)

    def test_meta_without_bbox_is_untouched(self):
        meta = {"id": "m1", "kind": "meta", "subtype": "gps", "bbox": None, "risk": 3}
        out = service._prepare_redaction_boxes([meta], self.w, self.h)
        self.assertIsNone(out[0]["bbox"])


if __name__ == "__main__":
    unittest.main()
