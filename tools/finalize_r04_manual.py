#!/usr/bin/env python3
"""Validate two independent R-04 human reviews and write the consensus summary."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

VALID = {"readable", "not_readable"}


def cohen_kappa(left: list[int], right: list[int]) -> float | None:
    if not left or len(left) != len(right):
        return None
    observed = sum(a == b for a, b in zip(left, right)) / len(left)
    left_positive = sum(left) / len(left)
    right_positive = sum(right) / len(right)
    expected = left_positive * right_positive + (1 - left_positive) * (1 - right_positive)
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)


def finalize(csv_path: Path) -> dict[str, Any]:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("manual review CSV is empty")

    left: list[int] = []
    right: list[int] = []
    problems: list[str] = []
    for index, row in enumerate(rows, start=1):
        reviewer_a = str(row.get("manual_reviewer_a") or "").strip().lower()
        reviewer_b = str(row.get("manual_reviewer_b") or "").strip().lower()
        if reviewer_a not in VALID or reviewer_b not in VALID:
            problems.append(f"row {index}: reviewer values must be readable/not_readable")
            continue
        adjudicated = str(row.get("manual_adjudicated") or "").strip().lower()
        if reviewer_a != reviewer_b and adjudicated not in VALID:
            problems.append(f"row {index}: disagreement requires manual_adjudicated")
            continue
        consensus = reviewer_a if reviewer_a == reviewer_b else adjudicated
        row["manual_consensus"] = consensus
        left.append(int(reviewer_a == "readable"))
        right.append(int(reviewer_b == "readable"))

    if problems:
        raise ValueError("; ".join(problems[:10]))

    reviewer_agreements = sum(a == b for a, b in zip(left, right))
    human_values = [
        1 if str(row["manual_consensus"]).lower() == "readable" else 0
        for row in rows
    ]
    primary_values = [int(row.get("primary_status") == "passed") for row in rows]
    tesseract_values = [int(row.get("tesseract_status") == "passed") for row in rows]
    summary = {
        "sample_size": len(rows),
        "reviewer_agreement_count": reviewer_agreements,
        "reviewer_agreement_rate": round(reviewer_agreements / len(rows), 4),
        "cohen_kappa": cohen_kappa(left, right),
        "human_readable": sum(human_values),
        "human_not_readable": len(rows) - sum(human_values),
        "primary_vs_human_agreement": round(
            sum(a == b for a, b in zip(primary_values, human_values)) / len(rows),
            4,
        ),
        "tesseract_vs_human_agreement": round(
            sum(a == b for a, b in zip(tesseract_values, human_values)) / len(rows),
            4,
        ),
        "manual_review_status": "complete_two_independent_reviewers",
    }

    output_csv = csv_path.with_name("manual-consensus.csv")
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary_path = csv_path.with_name("manual-summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize R-04 two-person manual review")
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = finalize(args.csv_path)
    except (OSError, ValueError) as exc:
        print(f"[R-04 manual] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
