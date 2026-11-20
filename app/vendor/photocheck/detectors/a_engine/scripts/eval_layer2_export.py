#!/usr/bin/env python
"""Layer-2 evaluation: re-inspect the EXPORTED file, not the working image.

Why this exists: `AUDIT.md` has exactly one FAIL (EV-02) and it is this layer.
Every other number in the project is produced from the in-memory image, which
proves the detector *found* things but says nothing about whether the file the
user walks away with is clean. "Masked in the middle image" and "masked in the
export" are different claims, and only the second one is the product.

What it does per image, using the app's own pipeline (not a reimplementation):

    1. save_upload  -> the same path a browser drag-and-drop takes
    2. detect       -> A's detections + B's meta
    3. process      -> real redaction, real export, real verification
    4. re-analyse the exported file with A's detector
    5. scan the exported file for surviving EXIF/XMP/IPTC

Step 4 is the layer-2 number. Step 5 is the part only the export can answer:
a redacted pixel can still carry the original GPS tag.

Runs inside the packaged app's runtime (it needs the vendored photocheck and
the bundled ExifTool):

    <app>/runtime/python.exe scripts/eval_layer2_export.py \
        --app-root C:/pc_new/PhotoCheck_App \
        --dataset  C:/pc_full/PhotoCheck/dataset/dev \
        --out      reports/layer2_export_20261114.json

Exit code is 0 when every exported file came back clean, 1 when any image kept
a detection or a metadata tag.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

A_KINDS = ("text", "qr", "face")


def _bootstrap(app_root: Path) -> None:
    """Make the packaged app importable, and point ExifTool at the bundled copy."""
    for rel in ("", "app", "app/vendor"):
        p = app_root / rel if rel else app_root
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    bundled = app_root / "runtime" / "exiftool" / "exiftool.exe"
    if bundled.is_file() and not os.environ.get("PHOTOCHECK_EXIFTOOL"):
        os.environ["PHOTOCHECK_EXIFTOOL"] = str(bundled)


def _load_engine():
    """Import A's pipeline from wherever this tree keeps it.

    Two layouts are in play: A's own repo has `core/` at the top level, while the
    packaged app vendors the same code as `photocheck.detectors.a_engine.core`.
    Importing the wrong one silently scores a different engine, so try the
    namespaced path first (the app is what actually produced the export) and
    report which one won.
    """
    try:
        from photocheck.detectors.a_engine.core.detect import analyze  # noqa: PLC0415

        return analyze, "photocheck.detectors.a_engine"
    except ImportError:
        from core.detect import analyze  # noqa: PLC0415

        return analyze, "core"


def _export_tags(exiftool: Path, path: Path) -> list[str]:
    """Metadata tags left in the exported file, ignoring benign container fields.

    `ImageSize`, `BitDepth`, `ColorType` and friends are structural: they are in
    every PNG and say nothing about the photographer. Anything from EXIF, XMP,
    IPTC, Photoshop or a PNG text chunk is a leak if it survived redaction.
    """
    if not exiftool.is_file():
        return ["<exiftool missing>"]
    try:
        out = subprocess.run(
            [str(exiftool), "-a", "-G1", "-s", "-EXIF:all", "-XMP:all", "-IPTC:all",
             "-Photoshop:all", "-PNG:tEXt", "-PNG:zTXt", "-PNG:iTXt", str(path)],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return [f"<exiftool error: {type(exc).__name__}>"]
    tags = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.endswith("tags in file") or line.startswith("[ExifTool]"):
            continue
        tags.append(line)
    return tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="layer-2: evaluate the exported files")
    parser.add_argument("--app-root", required=True, help="path to PhotoCheck_App")
    parser.add_argument("--dataset", required=True, help="path to dataset/dev")
    parser.add_argument("--out", default=None, help="write the per-image JSON here")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--method", default="solid")
    args = parser.parse_args(argv)

    app_root = Path(args.app_root).resolve()
    _bootstrap(app_root)

    from app import service  # noqa: PLC0415  (needs the path set up first)

    analyze, engine_name = _load_engine()

    exiftool = Path(os.environ["PHOTOCHECK_EXIFTOOL"])
    dev = Path(args.dataset)
    labels = sorted((dev / "labels").glob("*.json"))
    if args.limit:
        labels = labels[: args.limit]
    if not labels:
        raise SystemExit(f"no labels under {dev / 'labels'}")

    print(f"{len(labels)} labelled image(s) | app {app_root.name} | "
          f"engine {engine_name} | exiftool {exiftool.name}")
    print(f"{'image':20s}{'det':>4s}{'out':>5s}{'residual':>9s}{'tags':>5s}{'state':>10s}{'sec':>6s}")

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for lbl in labels:
        doc = json.loads(lbl.read_text(encoding="utf-8"))
        image_id = next((r.get("image_id") for r in doc
                         if r.get("record_type") == "ImageDescriptor"), lbl.stem)
        src = dev / "images" / f"{image_id}.jpg"
        if not src.is_file():
            cands = sorted((dev / "images").glob(f"{image_id}.*"))
            if not cands:
                failures.append({"image_id": image_id, "error": "image not found"})
                continue
            src = cands[0]

        started = time.perf_counter()
        try:
            payload = src.read_bytes()
            source_id = service.save_upload(src.name, payload)
            detected = service.detect(source_id)
            ids = [d["id"] for d in detected["detections"] if d["kind"] != "meta"]
            processed = service.process(
                source_id, ids, args.method, [], detected["analysis_id"], 1,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"image_id": image_id, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {image_id:20s}FAILED {type(exc).__name__}")
            continue
        elapsed = time.perf_counter() - started

        out_path = Path(processed.get("output_path") or "")
        if not out_path.is_file():
            failures.append({"image_id": image_id, "error": "no exported file"})
            continue

        # Layer 2a: does the exported file still contain anything A detects?
        try:
            again = analyze(str(out_path), image_id)
            residual = [d for d in again.detections if d["kind"] in A_KINDS and d["bbox"]]
        except Exception as exc:  # noqa: BLE001
            failures.append({"image_id": image_id, "error": f"re-analyse: {type(exc).__name__}: {exc}"})
            continue

        # Layer 2b: did the export strip metadata?
        tags = _export_tags(exiftool, out_path)

        rows.append(
            {
                "image_id": image_id,
                "detected": len(detected["detections"]),
                "selected": len(ids),
                "meta_count": detected["meta_count"],
                "disposal_state": processed.get("disposal_state"),
                "residual_count": len(residual),
                "residual": [
                    {"kind": d["kind"], "subtype": d["subtype"], "bbox": d["bbox"],
                     "risk": d.get("risk")}
                    for d in residual
                ],
                "metadata_tags": tags,
                "output": str(out_path),
                "seconds": round(elapsed, 1),
            }
        )
        state = str(processed.get("disposal_state"))
        print(f"  {image_id:20s}{len(detected['detections']):4d}{len(ids):5d}"
              f"{len(residual):9d}{len(tags):5d}{state:>10s}{elapsed:6.1f}")

    print("-" * 64)
    n = len(rows)
    if n:
        clean = [r for r in rows if r["residual_count"] == 0 and not r["metadata_tags"]]
        residual_total = sum(r["residual_count"] for r in rows)
        tag_total = sum(len(r["metadata_tags"]) for r in rows)
        passed = sum(1 for r in rows if r["disposal_state"] == "passed")
        times = [r["seconds"] for r in rows]
        print(f"images                {n}")
        print(f"export clean          {len(clean)}/{n} = {len(clean) / n:.1%}"
              f"   (residual detections 0 AND metadata tags 0)")
        print(f"residual detections   {residual_total} across {n} exports")
        print(f"surviving metadata    {tag_total} tag(s)")
        print(f"disposal_state passed {passed}/{n} = {passed / n:.1%}")
        print(f"per image             median {statistics.median(times):.1f}s, max {max(times):.1f}s")
    if failures:
        print(f"\n{len(failures)} image(s) could not be processed:")
        for f in failures:
            print(f"  {f['image_id']}: {f['error'][:110]}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "layer": 2,
                    "definition": "detect on the EXPORTED file + metadata scan of that file",
                    "app_root": str(app_root),
                    "engine": engine_name,
                    "dataset": str(dev),
                    "method": args.method,
                    "images": n,
                    "rows": rows,
                    "failures": failures,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwritten {args.out}")

    dirty = [r for r in rows if r["residual_count"] or r["metadata_tags"]]
    return 1 if (dirty or failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
