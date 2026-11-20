from __future__ import annotations

import unittest
from pathlib import Path

from tools import bench_reference, make_k03_reference


class K03ReferenceTests(unittest.TestCase):
    def test_reference_specs_match_frozen_contract(self) -> None:
        expected = [
            (900, 620, 4, 76),
            (900, 620, 16, 864),
            (1120, 1560, 5, 95),
            (1120, 1560, 20, 1080),
            (1600, 1200, 15, 810),
        ]
        actual = [(width, height, lines, chars) for _, width, height, lines, chars, _ in make_k03_reference.SPECS]
        self.assertEqual(actual, expected)

    def test_generated_lines_preserve_requested_character_count(self) -> None:
        for total, count in ((76, 4), (864, 16), (1080, 20)):
            lines = make_k03_reference._make_lines(total, count)
            self.assertLessEqual(sum(len(line) for line in lines), total)
            self.assertEqual(sum(len(line) for line in lines), total)

    def test_report_validator_rejects_missing_summary_field(self) -> None:
        report = {
            "version": "k03_ref_v1",
            "reference_manifest_sha256": "x",
            "host": {"power": "ac"},
            "config": {},
            "summary": {},
            "rows": [],
        }
        with self.assertRaisesRegex(ValueError, "summary missing"):
            bench_reference.validate_report(report)


if __name__ == "__main__":
    unittest.main()
