"""第二层评测产物的隐私与完整性边界。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import run_layer2  # noqa: E402


def detection(preview: str = "138****0000") -> dict:
    return {
        "contract_version": "photocheck/v4",
        "record_type": "Detection",
        "id": "det-phone-001",
        "image_id": "upload-hash",
        "kind": "text",
        "subtype": "phone",
        "bbox": [1, 2, 30, 10],
        "detector_score": 0.9,
        "evidence": "ocr_rule",
        "risk": 3,
        "details": {"masked_preview": preview},
    }


class PredictionPersistence(unittest.TestCase):
    def test_valid_prediction_is_rewritten_to_dataset_image_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_layer2.write_predictions(out, "image-dev-001", [detection()])
            saved = json.loads((out / "image-dev-001.json").read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["image_id"], "image-dev-001")

    def test_unmasked_phone_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(Exception):
                run_layer2.write_predictions(out, "image-dev-001", [detection("13800138000")])
            self.assertFalse((out / "image-dev-001.json").exists())


class Layer2Completeness(unittest.TestCase):
    def row(self, image_id: str, out: bool = True, meta_count: int | None = 0) -> dict:
        return {
            "image_id": image_id,
            "out": f"C:/tmp/{image_id}.jpg" if out else "",
            "plan": 1,
            "plan_regions": [{
                "id": f"redact:{image_id}",
                "finding_id": image_id,
                "bbox": [1, 2, 3, 4],
                "method": "solid",
                "subtype": "phone",
            }],
            "checks": 1,
            "check_fail": 0,
            "residuals": 0,
            "ms": 10,
            "export_meta_count": meta_count,
        }

    def test_all_images_exported_and_checked_is_ok(self):
        result = run_layer2.build_layer2_result([self.row("a"), self.row("b")], "solid")
        self.assertEqual(result["status"], "ok")

    def test_missing_export_cannot_be_ok(self):
        result = run_layer2.build_layer2_result(
            [self.row("a"), self.row("b", out=False, meta_count=None)], "solid"
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("缺导出 1 张", result["reason"])

    def test_metadata_check_failure_cannot_be_ok(self):
        result = run_layer2.build_layer2_result(
            [self.row("a"), self.row("b", meta_count=None)], "solid"
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("未完成元数据检查 1 张", result["reason"])

    def test_missing_plan_evidence_cannot_be_ok(self):
        bad = self.row("b")
        bad["plan_regions"] = []
        result = run_layer2.build_layer2_result([self.row("a"), bad], "solid")
        self.assertEqual(result["status"], "failed")
        self.assertIn("计划区域证据不全 1 张", result["reason"])


class PlanRegionEvidence(unittest.TestCase):
    def test_detection_subtype_is_added_and_manual_is_explicit(self):
        regions = [
            {
                "id": "redact:det-1",
                "finding_id": "det-1",
                "bbox": [1, 2, 3, 4],
                "method": "solid",
            },
            {
                "id": "manual-001",
                "finding_id": None,
                "bbox": [5, 6, 7, 8],
                "method": "solid",
            },
        ]
        out = run_layer2.plan_regions_for_evidence(
            regions, [{"id": "det-1", "subtype": "phone"}]
        )
        self.assertEqual(out[0]["subtype"], "phone")
        self.assertEqual(out[1]["subtype"], "manual")


if __name__ == "__main__":
    unittest.main(verbosity=2)
