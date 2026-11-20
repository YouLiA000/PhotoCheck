from __future__ import annotations

import unittest
import csv
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from tools import r04_second_ocr
from tools import finalize_r04_manual


def candidate(index: int, subtype: str) -> r04_second_ocr.Candidate:
    return r04_second_ocr.Candidate(
        image_id=f"image-{index:03d}",
        detection_id=f"det-{index:03d}",
        subtype=subtype,
        bbox=(10, 20, 100, 30),
        masked_preview="1**********0000",
        image_path=Path(f"image-{index:03d}.jpg"),
    )


class R04SamplingTests(unittest.TestCase):
    def test_stratified_sample_is_deterministic_and_has_exact_size(self) -> None:
        pool = [candidate(index, "phone") for index in range(25)]
        pool += [candidate(100 + index, "idcard") for index in range(12)]
        first = r04_second_ocr.stratified_sample(
            pool, phone_count=20, idcard_count=10, seed=7
        )
        second = r04_second_ocr.stratified_sample(
            pool, phone_count=20, idcard_count=10, seed=7
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 30)
        self.assertEqual(sum(item.subtype == "phone" for item in first), 20)
        self.assertEqual(sum(item.subtype == "idcard" for item in first), 10)

    def test_insufficient_stratum_fails_closed(self) -> None:
        pool = [candidate(index, "phone") for index in range(20)]
        pool += [candidate(100 + index, "idcard") for index in range(3)]
        with self.assertRaisesRegex(ValueError, "not enough idcard"):
            r04_second_ocr.stratified_sample(
                pool, phone_count=20, idcard_count=10, seed=7
            )

    def test_matching_requires_same_subtype_and_iou_threshold(self) -> None:
        item = candidate(1, "phone")
        exact = {
            "id": "pred-1",
            "kind": "text",
            "subtype": "phone",
            "bbox": [10, 20, 100, 30],
        }
        wrong_subtype = {**exact, "subtype": "idcard"}
        shifted = {**exact, "bbox": [100, 200, 100, 30]}
        self.assertEqual(r04_second_ocr.best_detection(item, [exact])["id"], "pred-1")
        self.assertIsNone(r04_second_ocr.best_detection(item, [wrong_subtype]))
        self.assertIsNone(r04_second_ocr.best_detection(item, [shifted]))

    def test_contact_sheet_handles_multiple_open_crops(self) -> None:
        crops = [
            ("image-001", "1**********0000", Image.new("RGB", (120, 32), "black")),
            ("image-002", "1**********1111", Image.new("RGB", (120, 32), "black")),
        ]
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "sheet.png"
            r04_second_ocr._make_contact_sheet(crops, destination)
            self.assertTrue(destination.is_file())
            with Image.open(destination) as generated:
                self.assertEqual(generated.format, "PNG")

    def test_manual_finalizer_requires_two_valid_independent_reviews(self) -> None:
        headers = [
            "sample_no",
            "primary_status",
            "tesseract_status",
            "manual_reviewer_a",
            "manual_reviewer_b",
            "manual_adjudicated",
        ]
        rows = [
            {
                "sample_no": 1,
                "primary_status": "passed",
                "tesseract_status": "passed",
                "manual_reviewer_a": "not_readable",
                "manual_reviewer_b": "not_readable",
                "manual_adjudicated": "",
            },
            {
                "sample_no": 2,
                "primary_status": "passed",
                "tesseract_status": "passed",
                "manual_reviewer_a": "not_readable",
                "manual_reviewer_b": "readable",
                "manual_adjudicated": "not_readable",
            },
        ]
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
            summary = finalize_r04_manual.finalize(path)
            self.assertTrue(path.with_name("manual-consensus.csv").is_file())
            self.assertTrue(path.with_name("manual-summary.json").is_file())
        self.assertEqual(summary["reviewer_agreement_count"], 1)
        self.assertEqual(summary["manual_review_status"], "complete_two_independent_reviewers")

    def test_manual_finalizer_rejects_blank_review(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            path.write_text(
                "sample_no,primary_status,tesseract_status,"
                "manual_reviewer_a,manual_reviewer_b,manual_adjudicated\n"
                "1,passed,passed,,,\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "reviewer values"):
                finalize_r04_manual.finalize(path)


if __name__ == "__main__":
    unittest.main()
