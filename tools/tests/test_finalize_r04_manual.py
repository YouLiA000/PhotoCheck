"""R-04 finalizer polarity and validation regressions."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.finalize_r04_manual import finalize  # noqa: E402

FIELDS = [
    "manual_reviewer_a",
    "manual_reviewer_b",
    "manual_adjudicated",
    "primary_status",
    "tesseract_status",
]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def row(
    reviewer_a: str,
    reviewer_b: str,
    *,
    adjudicated: str = "",
    primary: str = "passed",
    tesseract: str = "passed",
) -> dict[str, str]:
    return {
        "manual_reviewer_a": reviewer_a,
        "manual_reviewer_b": reviewer_b,
        "manual_adjudicated": adjudicated,
        "primary_status": primary,
        "tesseract_status": tesseract,
    }


class FinalizeR04Manual(unittest.TestCase):
    def finalize_rows(self, rows: list[dict[str, str]]) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            write_rows(path, rows)
            return finalize(path)

    def test_all_not_readable_with_all_passed_agrees(self) -> None:
        summary = self.finalize_rows([row("not_readable", "not_readable")] * 30)
        self.assertEqual(summary["human_not_readable"], 30)
        self.assertEqual(summary["primary_vs_human_agreement"], 1.0)
        self.assertEqual(summary["tesseract_vs_human_agreement"], 1.0)

    def test_all_readable_with_all_passed_disagrees(self) -> None:
        summary = self.finalize_rows([row("readable", "readable")] * 30)
        self.assertEqual(summary["human_readable"], 30)
        self.assertEqual(summary["primary_vs_human_agreement"], 0.0)
        self.assertEqual(summary["tesseract_vs_human_agreement"], 0.0)

    def test_both_sides_see_residual_agrees(self) -> None:
        summary = self.finalize_rows(
            [row("readable", "readable", primary="failed", tesseract="failed")]
        )
        self.assertEqual(summary["primary_vs_human_agreement"], 1.0)
        self.assertEqual(summary["tesseract_vs_human_agreement"], 1.0)

    def test_half_and_half_matching_agrees(self) -> None:
        rows = (
            [row("readable", "readable", primary="failed", tesseract="failed")] * 15
            + [row("not_readable", "not_readable")] * 15
        )
        summary = self.finalize_rows(rows)
        self.assertEqual(summary["primary_vs_human_agreement"], 1.0)
        self.assertEqual(summary["tesseract_vs_human_agreement"], 1.0)

    def test_blank_reviewer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            write_rows(path, [
                row("not_readable", "not_readable"),
                row("not_readable", ""),
            ])
            with self.assertRaisesRegex(ValueError, "row 2"):
                finalize(path)

    def test_disagreement_needs_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            write_rows(path, [row("readable", "not_readable")])
            with self.assertRaisesRegex(ValueError, "adjudicated"):
                finalize(path)

    def test_adjudicated_disagreement_is_accepted(self) -> None:
        summary = self.finalize_rows([
            row("not_readable", "not_readable"),
            row("readable", "not_readable", adjudicated="not_readable"),
        ])
        self.assertEqual(summary["reviewer_agreement_rate"], 0.5)
        self.assertEqual(summary["human_not_readable"], 2)
        self.assertEqual(summary["manual_independence_source"], "process_attestation_only")

    def test_summary_is_written_with_same_polarity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manual.csv"
            write_rows(path, [row("not_readable", "not_readable")] * 30)
            finalize(path)
            summary = json.loads(path.with_name("manual-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["primary_vs_human_agreement"], 1.0)


if __name__ == "__main__":
    unittest.main()
