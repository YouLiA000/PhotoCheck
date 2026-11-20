"""Run PhotoCheck over C's v4 dataset and emit B integration results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from photocheck.contract import build_redaction_plan, validate_records
from photocheck.core import ingest_image, process_and_export
from photocheck.detectors import analyze_a


def _parse_kinds(value: str) -> tuple[str, ...]:
    kinds = tuple(part.strip() for part in value.split(",") if part.strip())
    if not kinds or any(kind not in {"text", "qr", "face"} for kind in kinds):
        raise argparse.ArgumentTypeError("kinds must be a comma-separated text,qr,face list")
    return kinds


def _records_summary(result: Any) -> dict[str, Any]:
    return {
        "output": str(result.export.output_path),
        "output_sha256": result.export.output_sha256,
        "disposal_state": result.verification["disposal_state"],
        "checks": result.verification["checks"],
        "residual_count": len(result.verification["residuals"]),
        "full_file_recheck": result.full_recheck,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run B over C's dataset/{dev,lock} with the dataset image_id."
    )
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("--split", choices=("dev", "lock"), default="dev")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--image-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--kinds", type=_parse_kinds, default=("text", "qr", "face"))
    parser.add_argument(
        "--allow-lock",
        action="store_true",
        help="Explicitly allow a locked-set run after the freeze gate is satisfied.",
    )
    args = parser.parse_args()

    if args.split == "lock" and not args.allow_lock:
        parser.error("lock is sealed; pass --allow-lock only for an approved W8/W20 run")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    split_root = args.dataset_root / args.split
    image_root = split_root / "images"
    if not image_root.is_dir():
        parser.error(f"dataset image directory does not exist: {image_root}")

    requested = set(args.image_id)
    images = sorted(image_root.glob("*.jpg"))
    if requested:
        images = [path for path in images if path.stem in requested]
        missing = requested - {path.stem for path in images}
        if missing:
            parser.error(f"unknown image_id values: {', '.join(sorted(missing))}")
    if args.limit is not None:
        images = images[: args.limit]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or args.output_dir / "integration-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    processed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for image_path in images:
        image_id = image_path.stem
        try:
            with ingest_image(image_path, image_id=image_id) as ingested:
                envelope = analyze_a(
                    ingested.image,
                    image_id,
                    kinds=args.kinds,
                )
                detections = [
                    dict(item)
                    for item in (*ingested.detections, *envelope.detections)
                ]
                decisions = [
                    {
                        "contract_version": "photocheck/v4",
                        "record_type": "Decision",
                        "finding_id": detection["id"],
                        "action": "redact",
                        "revision": 1,
                    }
                    for detection in detections
                    if detection["kind"] != "meta"
                ]
                validate_records([ingested.descriptor, *detections, *decisions])
                plan = build_redaction_plan(
                    ingested.descriptor,
                    detections,
                    decisions,
                    revision=1,
                )
                result = process_and_export(
                    ingested,
                    plan,
                    output_dir=args.output_dir,
                    detections=detections,
                    decisions=decisions,
                    run_full_recheck=True,
                )
            processed.append(
                {
                    "image_id": image_id,
                    "detection_count": len(detections),
                    "detections_by_kind": {
                        kind: sum(item["kind"] == kind for item in detections)
                        for kind in ("meta", "text", "qr", "face")
                    },
                    "warnings": list(envelope.warnings),
                    "timings_ms": dict(envelope.timings_ms),
                    "result": _records_summary(result),
                }
            )
        except Exception as exc:  # noqa: BLE001 - report per-image input failures
            failures.append(
                {
                    "image_id": image_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    payload = {
        "dataset_root": str(args.dataset_root.resolve()),
        "split": args.split,
        "image_count": len(images),
        "processed_count": len(processed),
        "failure_count": len(failures),
        "processed": processed,
        "failures": failures,
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
