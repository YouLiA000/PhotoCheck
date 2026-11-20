"""标注 CSV 工具的测试。

    python -m unittest discover -s dataset/tools/tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dataset.tools import labels_csv  # noqa: E402

DEV = ROOT / "dataset" / "dev"


def rows_of(mutate=None):
    with open(DEV / "labels.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(__import__("csv").DictReader(fh))
    if mutate:
        mutate(rows)
    return rows


SIZES = {"image-dev-001": (1120, 1560)}


class RoundTrip(unittest.TestCase):
    def test_json_csv_json_is_identical(self):
        before = {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((DEV / "labels").glob("*.json"))
        }
        before_neg = json.loads((DEV / "negatives.json").read_text(encoding="utf-8"))

        tmp = Path(tempfile.mkdtemp()) / "dev"
        shutil.copytree(DEV, tmp)
        self.assertEqual(labels_csv.import_dir(tmp, write_back=True), 0)

        for stem, records in before.items():
            after = json.loads((tmp / "labels" / f"{stem}.json").read_text(encoding="utf-8"))
            self.assertEqual(records, after, f"{stem} 往返后不一致")
        self.assertEqual(
            before_neg, json.loads((tmp / "negatives.json").read_text(encoding="utf-8"))
        )


class ImportRules(unittest.TestCase):
    def test_plaintext_preview_rejected(self):
        def mutate(rows):
            for row in rows:
                if row["kind"] == "text":
                    row["masked_preview"] = "13800138000"
                    break

        with self.assertRaises(ValueError) as ctx:
            labels_csv.import_rows(rows_of(mutate), SIZES)
        self.assertIn("掩码", str(ctx.exception))

    def test_idcard_requires_tristate(self):
        def mutate(rows):
            for row in rows:
                if row["subtype"] == "idcard" and row["verdict"] == "positive":
                    row["idcard_check"] = ""
                    break

        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(mutate), SIZES)

    def test_qr_requires_decodable(self):
        def mutate(rows):
            for row in rows:
                if row["kind"] == "qr":
                    row["decodable"] = ""
                    break

        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(mutate), SIZES)

    def test_unknown_kind_rejected(self):
        def mutate(rows):
            rows[0]["kind"] = "plate"

        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(mutate), SIZES)

    def test_negative_rows_may_omit_verdict(self):
        # 规格卡字段表里没有 verdict 列，只用规格卡字段的人会把负样本写成空 kind
        rows = [
            {"file": "images/image-dev-001.jpg", "set": "dev", "scene_id": "scene-receipt",
             "kind": "", "subtype": "", "bbox": "10,20,200,40", "should_hide": "0",
             "difficulty": "easy", "note": "11 位但首位是 3", "verdict": "",
             "det_id": "", "masked_preview": "", "idcard_check": "", "decodable": "",
             "annotator": "", "checked": "0"},
        ]
        records, negatives = labels_csv.import_rows(rows, SIZES)
        self.assertEqual(len(negatives["image-dev-001"]), 1)
        self.assertEqual(records["image-dev-001"], records["image-dev-001"][:1])

    def test_risk_comes_from_rules_not_csv(self):
        rows = [
            {"file": "images/image-dev-001.jpg", "set": "dev", "scene_id": "scene-receipt",
             "kind": "text", "subtype": "phone", "bbox": "10,20,200,40", "should_hide": "1",
             "difficulty": "easy", "note": "", "verdict": "positive", "det_id": "",
             "masked_preview": "138****0000", "idcard_check": "", "decodable": "",
             "annotator": "", "checked": "0"},
        ]
        records, _ = labels_csv.import_rows(rows, SIZES)
        self.assertEqual(records["image-dev-001"][1]["risk"], 2)

    def test_bad_verdict_rejected(self):
        def mutate(rows):
            rows[0]["verdict"] = "maybe"

        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(mutate), SIZES)

    def test_missing_size_rejected(self):
        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(), {})

    def test_meta_row_needs_no_bbox(self):
        rows = [
            {"file": "images/image-dev-001.jpg", "set": "dev", "scene_id": "scene-receipt",
             "kind": "meta", "subtype": "gps", "bbox": "", "should_hide": "1",
             "difficulty": "easy", "note": "", "verdict": "positive", "det_id": "",
             "masked_preview": "", "idcard_check": "", "decodable": "",
             "annotator": "", "checked": "0"},
        ]
        records, _ = labels_csv.import_rows(rows, SIZES)
        det = records["image-dev-001"][1]
        self.assertIsNone(det["bbox"])
        self.assertEqual(det["id"], "det-image-dev-001-meta-gps")
        self.assertEqual(det["evidence"], "exif")

    def test_auto_id_counts_per_kind(self):
        base = {"file": "images/image-dev-001.jpg", "set": "dev", "scene_id": "scene-receipt",
                "should_hide": "1", "difficulty": "easy", "note": "", "verdict": "positive",
                "det_id": "", "masked_preview": "138****0000", "idcard_check": "",
                "decodable": "", "annotator": "", "checked": "0"}
        rows = [
            dict(base, kind="text", subtype="phone", bbox="10,20,200,40"),
            dict(base, kind="text", subtype="phone", bbox="10,80,200,40"),
        ]
        records, _ = labels_csv.import_rows(rows, SIZES)
        ids = [r["id"] for r in records["image-dev-001"][1:]]
        self.assertEqual(ids, ["det-image-dev-001-text-01", "det-image-dev-001-text-02"])


class FlowColumns(unittest.TestCase):
    def test_flow_columns_do_not_leak_into_records(self):
        records, _ = labels_csv.import_rows(rows_of(), labels_csv.load_sizes(DEV))
        for rec in records["image-dev-001"]:
            self.assertNotIn("annotator", rec)
            self.assertNotIn("checked", rec)
            self.assertNotIn("note", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CsvCoverage(unittest.TestCase):
    """CSV 必须覆盖**每一张**有真值的图，包括整图无对象的干净图。

    这条守的是一个真实缺口：干净负样本一行 Detection 都没有，导出时被整张跳过，
    于是 CSV 只有 29 张而真值有 35 张——谁按 CSV 清理一遍，就会把 6 张干净图
    当孤儿删掉，而它们正是误报率的分母。
    """

    def test_every_label_file_has_a_row(self):
        in_csv = {Path(r["file"]).stem for r in rows_of()}
        labels = {p.stem for p in (DEV / "labels").glob("*.json")}
        self.assertEqual(labels - in_csv, set(), "有真值但 CSV 里没有对应行")

    def test_clean_images_marked(self):
        cleans = {Path(r["file"]).stem for r in rows_of() if r["verdict"] == "clean"}
        self.assertGreaterEqual(len(cleans), 4, "干净负样本应该有显式 clean 行")
        for image_id in cleans:
            records = json.loads((DEV / "labels" / f"{image_id}.json").read_text(encoding="utf-8"))
            self.assertEqual([r for r in records if r["record_type"] == "Detection"], [],
                             f"{image_id} 标了 clean 但有 Detection")

    def test_clean_row_roundtrips_without_detections(self):
        rows = [r for r in rows_of() if r["verdict"] == "clean"][:1]
        records, negatives = labels_csv.import_rows(rows, labels_csv.load_sizes(DEV))
        image_id = Path(rows[0]["file"]).stem
        self.assertEqual(len(records[image_id]), 1, "只应有 ImageDescriptor")
        self.assertEqual(records[image_id][0]["record_type"], "ImageDescriptor")
        self.assertEqual(negatives.get(image_id), [])

    def test_clean_row_with_kind_rejected(self):
        def mutate(rows):
            row = next(r for r in rows if r["verdict"] == "clean")
            row["kind"] = "text"
            row["subtype"] = "phone"

        with self.assertRaises(ValueError):
            labels_csv.import_rows(rows_of(mutate), SIZES)

    def test_clean_image_cannot_also_have_objects(self):
        rows = rows_of()
        positive = next(r for r in rows if r["verdict"] == "positive")
        clean = dict(next(r for r in rows if r["verdict"] == "clean"))
        clean["file"] = positive["file"]

        with self.assertRaises(ValueError):
            labels_csv.import_rows([positive, clean], SIZES)

    def test_fractional_bbox_is_rejected(self):
        row = {
            "file": "images/image-dev-001.jpg", "set": "dev", "scene_id": "scene-receipt",
            "kind": "text", "subtype": "phone", "bbox": "10.5,20,200,40",
            "should_hide": "1", "difficulty": "easy", "note": "", "verdict": "positive",
            "det_id": "", "masked_preview": "138****0000", "idcard_check": "",
            "decodable": "", "annotator": "", "checked": "0",
        }
        with self.assertRaises(ValueError):
            labels_csv.import_rows([row], SIZES)
