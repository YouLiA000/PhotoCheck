#!/usr/bin/env python
"""Score A's detectors against C's datasets: the labelled dev set, and the real-photo set.

Why this exists: until C shipped `dataset/dev`, every number in A's reports came
from 10 synthetic images. "How does it do on real photos" had no answer. This
script turns C's artefacts into the shape `core.eval` expects and scores end to
end, so the answer stops being a guess.

Two datasets, two very different kinds of evidence - do not mix them up:

  dev/   v4 label files, geometry included  -> full scoring: recall, precision,
         IoU pairing, type confusion, per-subtype breakdown
  real/  14 phone photos of screens, only `manifest.csv` with `objects=phone=2;qr=1`
         -> COUNTING ONLY. There is no bbox, so no IoU and no precision. Calling
         this "recall" without saying so would overstate it.

Scope is A's three kinds only - `text`, `qr`, `face`. `meta` records are B's K-01
and are dropped here, not counted as misses: A never claims to detect EXIF.

Label format (C's, one JSON array per image):

    [ {record_type: ImageDescriptor, image_id, width, height},
      {record_type: Detection, kind, subtype, bbox, risk, ...}, ... ]

Usage:
    python scripts/eval_c_dev.py --dataset C:/pc_full/PhotoCheck/dataset/dev
    python scripts/eval_c_dev.py --dataset C:/pc_full/PhotoCheck/dataset/real --split real
    python scripts/eval_c_dev.py --dataset C:/pc_full/PhotoCheck/dataset --split all --json out.json

    # host missing the VC++ 2013 runtime (pyzbar cannot load) - verification aid only
    ... --fix-crt "C:/Program Files/Microsoft Office/root/Client"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.detect import analyze  # noqa: E402
from core.eval import DEFAULT_IOU, GtImage, GtObject, match_image  # noqa: E402

A_KINDS = ("text", "qr", "face")

# `manifest.csv` writes object counts as `phone=2;qr=1;barcode=1`. The map below
# is the whole translation between C's vocabulary and the v4 subtypes, so it is
# a function rather than a chain of ifs buried in the loop.
MANIFEST_KEYS = {
    "phone": ("text", "phone"),
    "idcard": ("text", "idcard"),
    "qr": ("qr", "*qr"),      # any qr_* subtype except the barcode one
    "barcode": ("qr", "qr_code"),
}


# --- dev: v4 labels, full geometric scoring ---------------------------------


def load_c_label(path: Path) -> tuple[GtImage, list[dict[str, Any]]]:
    """C's v4 record array -> (GtImage, meta records kept aside)."""
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path.name}: expected a JSON array")

    desc = next((r for r in records if r.get("record_type") == "ImageDescriptor"), None)
    if desc is None:
        raise ValueError(f"{path.name}: no ImageDescriptor")

    objects: list[GtObject] = []
    meta: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        if rec.get("record_type") != "Detection":
            continue
        kind = rec.get("kind")
        if kind == "meta":
            meta.append(rec)
            continue
        if kind not in A_KINDS:
            continue
        bbox = rec.get("bbox")
        if not bbox or len(bbox) != 4:
            # A non-meta record without geometry cannot be scored here; shouting
            # beats silently shrinking the denominator.
            print(f"[warn] {path.name}: {rec.get('id')} kind={kind} has no bbox, skipped",
                  file=sys.stderr)
            continue
        objects.append(
            GtObject(
                gt_id=str(rec.get("id") or f"g{i}"),
                kind=kind,
                subtype=str(rec.get("subtype")),
                bbox=tuple(int(v) for v in bbox),
            )
        )

    image_id = str(desc.get("image_id") or path.stem)
    gt = GtImage(
        image_id=image_id,
        image=f"{image_id}.jpg",
        width=int(desc["width"]),
        height=int(desc["height"]),
        objects=tuple(objects),
        negative=not objects,
    )
    return gt, meta


def _find_image(img_dir: Path, image_id: str) -> Path | None:
    """C names images `<image_id>.jpg`, but do not make the score depend on that."""
    direct = img_dir / f"{image_id}.jpg"
    if direct.is_file():
        return direct
    cands = sorted(img_dir.glob(f"{image_id}.*"))
    return cands[0] if cands else None


def run_labelled(dataset: Path, iou: float, limit: int) -> dict[str, Any]:
    """Full geometric scoring on a labelled split (dev or lock)."""
    img_dir, lbl_dir = dataset / "images", dataset / "labels"
    if not img_dir.is_dir() or not lbl_dir.is_dir():
        raise SystemExit(f"{dataset} does not look like a labelled split (need images/ and labels/)")

    label_files = sorted(lbl_dir.glob("*.json"))
    if limit:
        label_files = label_files[:limit]
    if not label_files:
        raise SystemExit(f"{lbl_dir} has no *.json label files")

    print(f"{len(label_files)} labelled image(s) from {dataset}")
    print(f"{'image':22s}{'gt':>4s}{'pred':>5s}{'tp':>4s}{'fp':>4s}{'fn':>4s}{'type':>5s}{'ms':>8s}")

    per_image: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    totals = {"gt": 0, "tp": 0, "fp": 0, "fn": 0, "type_ok": 0}
    by_kind: dict[str, dict[str, int]] = {}
    by_subtype: dict[str, dict[str, int]] = {}
    timings: list[float] = []
    meta_counts: dict[str, int] = {}
    confusions: list[dict[str, Any]] = []

    for lbl in label_files:
        gt, meta = load_c_label(lbl)
        for rec in meta:
            sub = str(rec.get("subtype", "?"))
            meta_counts[sub] = meta_counts.get(sub, 0) + 1

        img = _find_image(img_dir, gt.image_id)
        if img is None:
            failures.append({"image_id": gt.image_id, "error": "image file not found"})
            print(f"  {gt.image_id:20s} FAILED  image file not found")
            continue

        started = time.perf_counter()
        try:
            result = analyze(str(img), gt.image_id)
        except Exception as exc:  # noqa: BLE001
            failures.append({"image_id": gt.image_id, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {gt.image_id:20s} FAILED  {type(exc).__name__}")
            continue
        elapsed = (time.perf_counter() - started) * 1000
        timings.append(elapsed)

        ev = match_image(gt, result.detections, iou_threshold=iou, kinds=A_KINDS)
        totals["gt"] += ev.gt_count
        totals["tp"] += ev.tp
        totals["fp"] += ev.fp
        totals["fn"] += ev.fn
        totals["type_ok"] += ev.type_ok

        for o in gt.objects:
            by_kind.setdefault(o.kind, {"gt": 0, "tp": 0})["gt"] += 1
            by_subtype.setdefault(f"{o.kind}/{o.subtype}", {"gt": 0, "tp": 0, "type_ok": 0})["gt"] += 1
        for m in ev.matches:
            by_kind.setdefault(m.gt_kind, {"gt": 0, "tp": 0})["tp"] += 1
            s = by_subtype.setdefault(
                f"{m.gt_kind}/{m.gt_subtype}", {"gt": 0, "tp": 0, "type_ok": 0}
            )
            s["tp"] += 1
            if m.subtype_ok:
                s["type_ok"] += 1
            else:
                # A box that was found but misclassified is neither a miss nor a
                # false alarm; it only shows up as "type ok" dropping. Record the
                # confusion so the fix is aimed at the right card (K-05 vs K-06).
                confusions.append(
                    {
                        "image_id": gt.image_id,
                        "gt": f"{m.gt_kind}/{m.gt_subtype}",
                        "predicted": m.pred_subtype,
                        "iou": round(m.overlap, 3),
                    }
                )

        per_image.append(
            {
                **ev.to_dict(),
                "elapsed_ms": round(elapsed, 1),
                "timings": result.timings_ms,
                "warnings": result.warnings,
            }
        )
        print(f"  {gt.image_id:20s}{ev.gt_count:4d}{ev.pred_count:5d}{ev.tp:4d}"
              f"{ev.fp:4d}{ev.fn:4d}{ev.type_ok:5d}{elapsed:8.0f}")

    print("-" * 64)
    if totals["gt"]:
        recall = totals["tp"] / totals["gt"]
        precision = totals["tp"] / max(1, totals["tp"] + totals["fp"])
        print(f"GT {totals['gt']} | TP {totals['tp']} | FP {totals['fp']} | FN {totals['fn']}")
        print(f"geometry recall {recall:.1%} | precision {precision:.1%} | "
              f"type ok {totals['type_ok']}/{totals['tp']}")
        print(f"false alarms per image {totals['fp'] / max(1, len(per_image)):.2f}")
    if timings:
        print(f"elapsed per image: median {statistics.median(timings):.0f} ms, "
              f"max {max(timings):.0f} ms (whole pipeline, incl. OCR+QR+face)")
    if meta_counts:
        print(f"meta records in GT (B's K-01, not scored here): {meta_counts}")
    if confusions:
        print(f"\n{len(confusions)} box(es) found but classified wrongly:")
        for c in confusions:
            print(f"  {c['image_id']} {c['gt']} -> {c['predicted']}  (IoU {c['iou']})")
    if failures:
        print(f"\n{len(failures)} image(s) could not be analysed:")
        for f in failures:
            print(f"  {f['image_id']}: {f['error'][:100]}")

    print("\nby kind:")
    for k, v in sorted(by_kind.items()):
        print(f"  {k:6s} {v['tp']}/{v['gt']} = {v['tp'] / max(1, v['gt']):.0%}")
    print("\nby subtype:")
    for k, v in sorted(by_subtype.items()):
        print(f"  {k:22s} {v['tp']}/{v['gt']} = {v['tp'] / max(1, v['gt']):.0%}"
              f"  (type ok {v['type_ok']})")

    return {
        "mode": "labelled",
        "dataset": str(dataset),
        "totals": totals,
        "by_kind": by_kind,
        "by_subtype": by_subtype,
        "confusions": confusions,
        "failures": failures,
        "per_image": per_image,
    }


# --- real: manifest.csv, count-only ----------------------------------------


def parse_manifest_objects(text: str) -> dict[str, int]:
    """`"phone=2;qr=1;barcode=1"` -> {"phone": 2, "qr": 1, "barcode": 1}.

    Unknown keys are kept rather than dropped: if C adds a new object type, the
    report should show it as unscored instead of quietly losing it from the total.
    """
    out: dict[str, int] = {}
    for key, count in re.findall(r"(\w+)\s*=\s*(\d+)", text or ""):
        out[key.lower()] = out.get(key.lower(), 0) + int(count)
    return out


def count_predictions(detections: list[dict[str, Any]]) -> dict[str, int]:
    """Group predictions into the same buckets `manifest.csv` uses."""
    out = {"phone": 0, "idcard": 0, "qr": 0, "barcode": 0}
    for det in detections:
        kind, sub = det.get("kind"), str(det.get("subtype"))
        if kind == "text" and sub in ("phone", "idcard"):
            out[sub] += 1
        elif kind == "qr" and sub == "qr_code":
            out["barcode"] += 1
        elif kind == "qr" and sub.startswith("qr_"):
            out["qr"] += 1
    return out


def run_real(real_dir: Path, limit: int) -> dict[str, Any]:
    """Count-only scoring against `manifest.csv`.

    No bbox exists in the manifest, so there is nothing to pair and no IoU to
    compute. Reporting this as "recall" is only honest if the column says
    "counted", which is why every line below prints `found/expected`.
    """
    manifest = real_dir / "manifest.csv"
    if not manifest.is_file():
        raise SystemExit(f"{manifest} not found - real/ needs manifest.csv to be scoreable")

    with manifest.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if limit:
        rows = rows[:limit]
    if not rows:
        raise SystemExit(f"{manifest} has no rows")

    print(f"{len(rows)} real photo(s) from {real_dir}  (count-only: no bbox in manifest)")
    print(f"{'file':14s}{'expected':30s}{'got phone/id/qr/bc':22s}notes")

    per_image: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    want_total = {"phone": 0, "idcard": 0, "qr": 0, "barcode": 0}
    got_total = {"phone": 0, "idcard": 0, "qr": 0, "barcode": 0}
    extra_total = {"phone": 0, "idcard": 0, "qr": 0, "barcode": 0}

    for row in rows:
        name = (row.get("file") or "").strip()
        if not name:
            continue
        want = parse_manifest_objects(row.get("objects", ""))

        img = real_dir / name
        if not img.is_file():
            cands = sorted(real_dir.glob(f"{Path(name).stem}.*"))
            if not cands:
                failures.append({"file": name, "error": "image not found"})
                continue
            img = cands[0]

        try:
            result = analyze(str(img), Path(name).stem)
        except Exception as exc:  # noqa: BLE001
            failures.append({"file": name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {name:14s}FAILED {type(exc).__name__}")
            continue

        got = count_predictions(result.detections)
        notes = []
        for key, expected in want.items():
            bucket = key if key in got_total else None
            if bucket is None:
                notes.append(f"{key}=? (unscored)")
                continue
            want_total[bucket] += expected
            got_total[bucket] += got[bucket]
            if got[bucket] > expected:
                extra_total[bucket] += got[bucket] - expected
            if got[bucket] != expected:
                notes.append(f"{key} {expected}->{got[bucket]}")

        per_image.append(
            {
                "file": name,
                "expected": want,
                "found": got,
                "detections": [
                    {"kind": d.get("kind"), "subtype": d.get("subtype")} for d in result.detections
                ],
            }
        )
        exp_text = ";".join(f"{k}={v}" for k, v in want.items()) or "-"
        got_text = f"{got['phone']}/{got['idcard']}/{got['qr']}/{got['barcode']}"
        print(f"  {name:14s}{exp_text:30s}{got_text:22s}{' '.join(notes)}")

    print("-" * 64)
    print("counted recall (manifest has no bbox, so this is not geometric recall):")
    for key in ("phone", "idcard", "qr", "barcode"):
        if want_total[key] or got_total[key]:
            rate = got_total[key] / max(1, want_total[key])
            print(f"  {key:8s} {got_total[key]:3d}/{want_total[key]:<3d} = {rate:5.1%}"
                  f"   over-reported {extra_total[key]}")
    if failures:
        print(f"\n{len(failures)} photo(s) could not be analysed:")
        for f in failures:
            print(f"  {f['file']}: {f['error'][:100]}")

    return {
        "mode": "real-count-only",
        "dataset": str(real_dir),
        "expected": want_total,
        "found": got_total,
        "over_reported": extra_total,
        "per_image": per_image,
        "failures": failures,
    }


# --- entry point ------------------------------------------------------------


def _pick_split(dataset: Path, requested: str) -> str:
    """`auto` resolves to dev when both look plausible, because dev is scoreable."""
    if requested != "auto":
        return requested
    if (dataset / "labels").is_dir():
        return "dev"
    if (dataset / "manifest.csv").is_file():
        return "real"
    if (dataset / "dev").is_dir() or (dataset / "real").is_dir():
        return "all"
    raise SystemExit(
        f"cannot tell what {dataset} is: pass --split dev|real|all "
        f"(looked for labels/, manifest.csv, dev/, real/)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="score A's detectors on C's datasets")
    parser.add_argument("--dataset", required=True,
                        help="dataset/dev, dataset/real, or the dataset root")
    parser.add_argument("--split", default="auto", choices=["auto", "dev", "real", "all"])
    parser.add_argument("--limit", type=int, default=0, help="max images per split")
    parser.add_argument("--iou", type=float, default=DEFAULT_IOU)
    parser.add_argument("--json", default=None, help="write the full report here")
    parser.add_argument(
        "--fix-crt",
        default=None,
        help="directory holding 64-bit MSVCR120.dll; makes pyzbar loadable on a host "
             "that lacks the VC++ 2013 runtime (verification aid, not a product dependency)",
    )
    args = parser.parse_args(argv)

    if args.fix_crt:
        if os.path.isdir(args.fix_crt):
            os.add_dll_directory(args.fix_crt)
            print(f"[note] added DLL directory {args.fix_crt!r}")
        else:
            print(f"[warn] --fix-crt {args.fix_crt!r} is not a directory, ignored", file=sys.stderr)

    # Report the backend up front: cv2 fallback and pyzbar disagree on small codes
    # in cluttered photos, so a recall number without the backend is not comparable.
    from core.detect.qr import backend as qr_backend

    print(f"qr backend: {qr_backend()}")

    dataset = Path(args.dataset)
    split = _pick_split(dataset, args.split)
    reports: dict[str, Any] = {}

    if split in ("dev", "all"):
        dev_dir = dataset if (dataset / "labels").is_dir() else dataset / "dev"
        reports["dev"] = run_labelled(dev_dir, args.iou, args.limit)
        print()

    if split in ("real", "all"):
        real_dir = dataset if (dataset / "manifest.csv").is_file() else dataset / "real"
        reports["real"] = run_real(real_dir, args.limit)

    if args.json:
        out = reports if len(reports) > 1 else next(iter(reports.values()))
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
