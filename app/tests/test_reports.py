"""处置报告的落盘（TXT / JSON）。

报告是这套东西的"凭证"：图交出去，别人凭什么信你擦过了？
所以它必须和脱敏图一起落盘，而不是只活在浏览器里。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import service


def sample_report(**over) -> dict:
    report = {
        "analysis_id": "a1",
        "disposal_state": "passed",
        "checks": [
            {"region_id": "redact:det-phone-001", "status": "passed", "method": "a_ocr_probe"},
            {"region_id": "redact:det-qr-001", "status": "pending",
             "method": "semantic_verifier_pending"},
        ],
        "residuals": [],
        "full_recheck": {"detections": [], "unplanned_count": 0},
        "export": {
            "name": "photocheck-abc.jpg", "sha256": "deadbeef", "source_sha256": "cafebabe",
            "source_format": "JPEG", "pixel_mean_abs_diff": 0.42,
        },
        "plan_items": 2,
        "plan_revision": 1,
        "decisions": {"redact": 1, "retain": 1},
        "manual_regions": 0,
        "elapsed_ms": 7000,
        "warnings": [],
        "undecoded_qr": [],
    }
    report.update(over)
    return report


class ReportFiles(unittest.TestCase):
    def test_writes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = service._write_reports(
                out, sample_report(), {"image_id": "image-x", "width": 900, "height": 1100}
            )
            self.assertTrue(files["txt"].is_file())
            self.assertTrue(files["json"].is_file())

    def test_txt_is_readable_and_has_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = service._write_reports(
                Path(tmp), sample_report(), {"image_id": "image-x", "width": 9, "height": 9}
            )
            text = files["txt"].read_text(encoding="utf-8")
        for section in ("处置状态", "逐区域复检", "整图重扫", "导出"):
            self.assertIn(section, text)
        self.assertIn("本次计划中待处理区域未发现残留", text)
        self.assertIn("待确认", text, "pending 区域必须如实写成待确认，不能混成通过")

    def test_txt_warns_about_undecoded_qr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = service._write_reports(
                Path(tmp), sample_report(undecoded_qr=[[1, 2, 3, 4]]),
                {"image_id": "i", "width": 9, "height": 9},
            )
            text = files["txt"].read_text(encoding="utf-8")
        self.assertIn("二维码形状未被遮盖", text)

    def test_json_roundtrips_key_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = service._write_reports(
                Path(tmp), sample_report(), {"image_id": "image-x", "width": 900, "height": 1100}
            )
            payload = json.loads(files["json"].read_text(encoding="utf-8"))
        self.assertEqual(payload["disposal_state"], "passed")
        self.assertEqual(payload["image"]["image_id"], "image-x")
        self.assertEqual(payload["export"]["sha256"], "deadbeef")
        self.assertIn("generated_at", payload)

    def test_state_label_is_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = service._write_reports(
                Path(tmp), sample_report(disposal_state="not_applicable"),
                {"image_id": "i", "width": 1, "height": 1},
            )
            text = files["txt"].read_text(encoding="utf-8")
        self.assertIn("无需处置", text, "报告是给人看的，状态不该是英文枚举")


if __name__ == "__main__":
    unittest.main()
