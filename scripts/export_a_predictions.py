"""Export A detections in the per-image format consumed by ``eval/eval.py``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photocheck.contract import validate_records
from photocheck.core import ingest_image
from photocheck.detectors import analyze_a


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run A over labelled dataset images and write v4 prediction JSON."
    )
    parser.add_argument("--data", type=Path, default=ROOT / "dataset" / "dev")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--kinds", default="text,qr,face")
    args = parser.parse_args()

    kinds = tuple(item.strip() for item in args.kinds.split(",") if item.strip())
    if not kinds or any(kind not in {"text", "qr", "face"} for kind in kinds):
        parser.error("--kinds must be a comma-separated subset of text,qr,face")

    labels_dir = args.data / "labels"
    images_dir = args.data / "images"
    label_files = sorted(labels_dir.glob("*.json"))
    if not label_files:
        parser.error(f"no labels found under {labels_dir}")
    args.out.mkdir(parents=True, exist_ok=True)

    written = 0
    failures: list[dict[str, str]] = []
    for label_path in label_files:
        image_id = label_path.stem
        image_path = next(
            (
                path
                for path in (
                    images_dir / f"{image_id}.jpg",
                    images_dir / f"{image_id}.jpeg",
                    images_dir / f"{image_id}.png",
                )
                if path.is_file()
            ),
            None,
        )
        if image_path is None:
            failures.append({"image_id": image_id, "error": "image file not found"})
            continue
        try:
            with ingest_image(image_path, image_id=image_id) as ingested:
                envelope = analyze_a(ingested.image, image_id, kinds=kinds)
                records = [
                    dict(ingested.descriptor),
                    *(dict(item) for item in ingested.detections),
                    *(dict(item) for item in envelope.detections),
                ]
            validate_records(records)
            (args.out / f"{image_id}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - report per-image input/runtime failures
            failures.append(
                {"image_id": image_id, "error": f"{type(exc).__name__}: {exc}"}
            )

    summary = {
        "data": str(args.data.resolve()),
        "output": str(args.out.resolve()),
        "label_count": len(label_files),
        "written": written,
        "failure_count": len(failures),
        "failures": failures,
        "kinds": list(kinds),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
