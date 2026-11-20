"""第三层人工判读表的边界测试。

这一层的核心风险不是算错，而是**把空表或部分填写的表当成通过**。
所以测试只锁三件事：没填不给过、填了能给、误遮数不能超过遮盖数。
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import make_layer3_sheet  # noqa: E402

COLUMNS = ["image_id", "state", "change_pct", "redactions", "detections",
           "residuals", "affects_reading", "over_redacted", "note"]


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


class ImageMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.layer2 = self.tmp / "layer2.json"

    def row(self, image_id="image-dev-001", redactions=2,
            affects="", over="", state="passed"):
        return {
            "image_id": image_id, "state": state, "change_pct": "1.75",
            "redactions": str(redactions), "detections": "6", "residuals": "0",
            "affects_reading": affects, "over_redacted": over, "note": "",
        }

    def write_sources(self, rows: list[dict]) -> None:
        payload = {
            "status": "ok",
            "items": [
                {
                    "image_id": row["image_id"],
                    "plan": int(row["redactions"]),
                    "out": str(self.tmp / "exports" / f"{row['image_id']}.jpg"),
                    "plan_regions": [
                        {
                            "id": f"redact:{row['image_id']}:{index}",
                            "finding_id": f"det-{index}",
                            "bbox": [index, index, 10, 10],
                            "method": "solid",
                            "subtype": "phone",
                        }
                        for index in range(1, int(row["redactions"]) + 1)
                    ],
                }
                for row in rows
            ],
        }
        self.layer2.write_text(json.dumps(payload), encoding="utf-8")

    def run_it(self, rows: list[dict], source_rows: list[dict] | None = None):
        src = self.tmp / "review.csv"
        out = self.tmp / "layer3.json"
        write_csv(src, rows)
        self.write_sources(source_rows or rows)
        code = make_layer3_sheet.from_image_csv(
            Namespace(
                from_image_csv=str(src),
                out_json=str(out),
                layer2=str(self.layer2),
            )
        )
        return code, out

    def test_unfilled_is_rejected(self):
        code, out = self.run_it([self.row(), self.row("image-dev-002", 1)])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists(), "没填完就生成 JSON 等于用空表换通过")

    def test_filled_is_accepted(self):
        code, out = self.run_it([
            self.row(affects="0", over="0"),
            self.row("image-dev-021", redactions=0, state="not_applicable",
                     affects="0", over="0"),
        ])
        self.assertEqual(code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["review_unit"], "image")
        self.assertEqual(payload["summary"]["images"], 2)
        self.assertEqual(payload["summary"]["regions_reviewed"], 2)
        self.assertEqual(payload["summary"]["over_redacted"], 0)

    def test_more_over_than_redactions_is_rejected(self):
        code, out = self.run_it([self.row(affects="1", over="9")])
        self.assertEqual(code, 1, "一处遮盖不可能数出 9 个误遮对象")
        self.assertFalse(out.exists())

    def test_clean_image_cannot_have_over_redaction(self):
        code, _ = self.run_it([
            self.row("image-dev-021", redactions=0, state="not_applicable",
                     affects="0", over="1"),
        ])
        self.assertEqual(code, 1, "没有遮盖却有误遮，数据自相矛盾")

    def test_partial_filled_table_is_rejected_against_layer2(self):
        sources = [self.row(), self.row("image-dev-002", 1)]
        code, out = self.run_it([self.row(affects="0", over="0")], source_rows=sources)
        self.assertEqual(code, 1, "少判一张图不能生成 ok")
        self.assertFalse(out.exists())

    def test_redactions_must_match_layer2_plan(self):
        code, out = self.run_it(
            [self.row(redactions=1, affects="0", over="0")],
            source_rows=[self.row(redactions=2)],
        )
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


class RegionMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.layer2 = self.tmp / "layer2.json"
        self.layer2.write_text(json.dumps({
            "status": "ok",
            "items": [{
                "image_id": "image-dev-001",
                "out": str(self.tmp / "out.jpg"),
                "plan_regions": [{
                    "id": "redact:det-phone-001",
                    "finding_id": "det-phone-001",
                    "subtype": "phone",
                    "bbox": [10, 20, 100, 30],
                    "method": "solid",
                }],
            }],
        }), encoding="utf-8")

    def review_row(self):
        return {
            "row": "1",
            "image_id": "image-dev-001",
            "finding_id": "det-phone-001",
            "subtype": "phone",
            "bbox": "10,20,100,30",
            "affects_reading": "0",
            "over_redacted": "0",
            "note": "",
        }

    def run_it(self, rows: list[dict]):
        src = self.tmp / "region-review.csv"
        out = self.tmp / "region-layer3.json"
        with src.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=make_layer3_sheet.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        code = make_layer3_sheet.from_csv(Namespace(
            from_csv=str(src),
            out_json=str(out),
            layer2=str(self.layer2),
        ))
        return code, out

    def test_complete_region_review_generates_ok(self):
        code, out = self.run_it([self.review_row()])
        self.assertEqual(code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["provenance"]["expected_regions"], 1)

    def test_missing_region_is_rejected(self):
        code, out = self.run_it([])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_changed_bbox_is_rejected(self):
        row = self.review_row()
        row["bbox"] = "0,0,1,1"
        code, out = self.run_it([row])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
