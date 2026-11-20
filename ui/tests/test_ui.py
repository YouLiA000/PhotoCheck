"""界面层测试（K-11）。

重点不是测 Gradio，而是测**界面会不会把原文漏出去**——这是 UI 最容易犯的错：
清单里为了"方便用户确认"顺手显示完整号码，报告截图一转手就成了新的泄露源。

    python -m unittest discover -s ui/tests -v
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from contract.risk_explain import EVIDENCE_LABEL  # noqa: E402
from contract.risk_rules import detection_risk  # noqa: E402
from contract.validate_records import validate_records  # noqa: E402
from ui import datasource  # noqa: E402

FULL_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
FULL_IDCARD = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")


class MaskingRedline(unittest.TestCase):
    def test_display_rows_never_contain_full_numbers(self):
        for sample in datasource.list_samples():
            rows = datasource.display_rows(datasource.load_records(sample))
            for row in rows:
                for cell in row:
                    text = str(cell)
                    with self.subTest(sample=sample, cell=text):
                        self.assertIsNone(FULL_PHONE.search(text), "清单里出现了完整手机号")
                        self.assertIsNone(FULL_IDCARD.search(text), "清单里出现了完整身份证号")

    def test_panel_never_contains_full_numbers(self):
        for sample in datasource.list_samples():
            text = datasource.panel_markdown(datasource.load_records(sample))
            with self.subTest(sample=sample):
                self.assertIsNone(FULL_PHONE.search(text))
                self.assertIsNone(FULL_IDCARD.search(text))

    def test_panel_does_not_promise_safety(self):
        # 规格卡 §0.5 红线 3：不做安全承诺
        banned = ["已经处理干净", "可以安全分享", "无隐私风险", "已确保安全"]
        text = datasource.panel_markdown(datasource.load_records("image-dev-001"))
        for word in banned:
            self.assertNotIn(word, text)


class Rendering(unittest.TestCase):
    def test_draw_boxes_keeps_size_and_type(self):
        sample = "image-dev-014"
        with Image.open(datasource.image_path(sample)) as im:
            base = im.convert("RGB")
        drawn = datasource.draw_boxes(base, datasource.load_records(sample))
        self.assertEqual(drawn.size, base.size)
        self.assertEqual(drawn.mode, "RGB")
        # 只比左上角几个像素会误判（那张图左上本来就是纯色色块），整图比一次
        self.assertNotEqual(drawn.tobytes(), base.tobytes(), "画完跟原图一样，说明框没画上")

    def test_meta_findings_have_no_bbox_but_still_listed(self):
        rows = datasource.display_rows(datasource.load_records("image-dev-001"))
        meta_rows = [r for r in rows if r[1] == "元数据"]
        self.assertGreaterEqual(len(meta_rows), 3, "带 EXIF 的图应列出 3 条元数据发现项")
        for row in meta_rows:
            self.assertEqual(row[6], "整图")


class RiskColumns(unittest.TestCase):
    """K-10：severity 与 evidence 必须分两列；分值只给数字，不翻译成等级。"""

    def test_every_row_has_evidence_label(self):
        for sample in datasource.list_samples():
            for row in datasource.display_rows(datasource.load_records(sample)):
                with self.subTest(sample=sample, row=row):
                    self.assertIn(row[4], EVIDENCE_LABEL.values())

    def test_risk_column_is_number_not_level_word(self):
        # 「高/中/低」是整图等级；单对象只给 1/2/3，混用会被读成"这个手机号是高风险"
        for row in datasource.display_rows(datasource.load_records("image-dev-014")):
            with self.subTest(row=row):
                self.assertRegex(str(row[3]).rstrip(" *"), r"^[123]$")

    def test_row_risk_matches_rules(self):
        # 界面显示的分必须等于规则算的分，不能照抄检测器填的值
        for sample in datasource.list_samples():
            records = datasource.load_records(sample)
            for row, rec in zip(datasource.display_rows(records), datasource.detections(records)):
                with self.subTest(sample=sample, subtype=rec["subtype"]):
                    want = detection_risk(rec["kind"], rec["subtype"], rec.get("details") or {})
                    self.assertEqual(str(row[3]).rstrip(" *"), str(want))


class Samples(unittest.TestCase):
    def test_every_sample_loads_and_validates(self):
        samples = datasource.list_samples()
        self.assertGreaterEqual(len(samples), 30, "可选样本太少，界面没什么可演示的")
        for sample in samples:
            with self.subTest(sample=sample):
                report = validate_records(datasource.load_records(sample))
                self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_clean_image_yields_empty_table(self):
        rows = datasource.display_rows(datasource.load_records("image-dev-021"))
        self.assertEqual(rows, [], "干净图不该有任何发现项")


class InputGuards(unittest.TestCase):
    """界面不能把坏数据直接送进展示层，也不能把完整号码写进异常信息。"""

    def test_invalid_truth_is_rejected_before_display(self):
        records = copy.deepcopy(datasource.load_records("image-dev-001"))
        target = next(r for r in records if r.get("kind") == "text")
        target["details"]["masked_preview"] = "13800138000"

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "image-dev-999.json").write_text(
                json.dumps(records, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(datasource, "LABELS", Path(tmp)):
                with self.assertRaises(ValueError) as ctx:
                    datasource.load_records("image-dev-999")

        message = str(ctx.exception)
        self.assertIn("契约", message)
        self.assertIsNone(FULL_PHONE.search(message), "异常信息不能带出完整号码")

    def test_non_image_upload_returns_readable_status(self):
        from ui import app

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp, "not-an-image.jpg")
            broken.write_text("this is not an image", encoding="utf-8")
            result = app.detect(None, str(broken), progress=lambda *a, **k: None)

        self.assertEqual(len(result), 4)
        self.assertIsNone(result[0])
        self.assertIn("无法读取图片", result[2])
        self.assertEqual(result[3], result[2])

    def test_exports_never_overwrite_same_second(self):
        from datetime import datetime
        from ui import app

        box = {"id": "manual-001", "bbox": [0, 0, 10, 10], "method": "solid"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "ROOT", Path(tmp)), patch.object(app, "datetime") as clock:
                clock.now.return_value = datetime(2026, 11, 2, 21, 0, 0)
                first = app.export_pack("image-dev-001", {}, [box])
                second = app.export_pack("image-dev-001", {}, [box])

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second, "同一秒的两次导出不能指向同一个文件")
            self.assertTrue(Path(first).exists())
            self.assertTrue(Path(second).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SwitchSample(unittest.TestCase):
    """换样本的清理契约。

    这条测的不是"界面好看"，是**数据会不会挂错图**：`export_pack` 读的是 state
    而不是下拉框，`prev_state` 里存着上一张图的决定。换图不清它，用户直接点导出，
    就会把 A 图的决定挂在 B 图的 image_id 上发给 B——payload 合法但内容错。
    """

    def test_returns_exactly_eight_values(self):
        """返回值数量必须等于 build() 里 outputs 的槽位数，否则 Gradio 会静默错位。"""
        from ui import app

        out = app.switch_sample(datasource.list_samples()[0])
        self.assertEqual(len(out), 8, "switch_sample 的返回数必须与 outputs 一致")

    def test_clears_previous_decisions_and_boxes(self):
        """prev_state 与 boxes_state 必须回到空——它们会被导出直接读走。"""
        from ui import app

        picker, hint, d_table, d_summary, prev, boxes, b_table, b_msg = app.switch_sample(
            datasource.list_samples()[0]
        )
        self.assertEqual(prev, {}, "prev_state 没清空 → 导出会串图")
        self.assertEqual(boxes, [], "boxes_state 没清空 → 补框会串图")
        self.assertEqual(list(d_table), [])
        self.assertEqual(list(b_table), [])

    def test_hint_shows_which_sample(self):
        """处置页必须显示当前是哪张图，否则换过样本后容易以为还是上一张。"""
        from ui import app

        sample_id = datasource.list_samples()[0]
        _picker, hint, *_ = app.switch_sample(sample_id)
        self.assertIn(sample_id, hint)
