#!/usr/bin/env python3
"""Prepare locally authorised face photographs without publishing the images."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "dataset" / "real" / "faces"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_detector(image: Image.Image) -> list[Any]:
    from photocheck.detectors.a_engine.core.detect import face

    return face.detect_faces(image)


def _best_orientation(
    image: Image.Image,
    detector: Callable[[Image.Image], list[Any]],
) -> tuple[Image.Image, int, int]:
    choices = []
    for angle in (0, 90, 180, 270):
        candidate = image.rotate(angle, expand=True)
        choices.append((len(detector(candidate)), -abs(angle), angle, candidate))
    count, _, angle, chosen = max(choices, key=lambda item: (item[0], item[1]))
    return chosen, angle, count


def prepare(
    source: Path,
    output: Path = DEFAULT_OUTPUT,
    *,
    detector: Callable[[Image.Image], list[Any]] | None = None,
) -> dict[str, Any]:
    files = sorted(
        path
        for path in source.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not files:
        raise ValueError("authorised face-photo directory contains no images")
    detector = detector or _default_detector
    images = output / "images"
    images.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, path in enumerate(files, start=1):
        with Image.open(path) as original:
            normalized = ImageOps.exif_transpose(original).convert("RGB")
            normalized, rotation, detected = _best_orientation(normalized, detector)
            clean = Image.new("RGB", normalized.size)
            clean.paste(normalized)
        face_id = f"face-dev-{index:03d}"
        destination = images / f"{face_id}.jpg"
        clean.save(destination, format="JPEG", quality=95, exif=b"")
        rows.append(
            {
                "face_id": face_id,
                "file": destination.relative_to(output).as_posix(),
                "truth_faces": 1,
                "condition": "frontal_or_near_frontal" if detected else "profile_or_partial",
                "detected_faces": detected,
                "rotation_applied": rotation,
                "width": clean.width,
                "height": clean.height,
                "source_sha256": _sha256(path),
                "prepared_sha256": _sha256(destination),
                "authorization": "user_confirmed_2026-11-15",
            }
        )

    manifest = output / "manifest.local.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "version": "face_authorized_local_v1",
        "source_image_count": len(rows),
        "truth_face_count": len(rows),
        "detected_face_count": sum(row["detected_faces"] for row in rows),
        "frontal_or_near_frontal": sum(
            row["condition"] == "frontal_or_near_frontal" for row in rows
        ),
        "profile_or_partial": sum(row["condition"] == "profile_or_partial" for row in rows),
        "required_face_count": 20,
        "complete_required_count": len(rows) >= 20,
        "publication": "source images and local manifest must not be committed",
    }
    (output / "summary.local.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare authorised local face samples")
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    summary = prepare(Path(args.source), Path(args.out))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
