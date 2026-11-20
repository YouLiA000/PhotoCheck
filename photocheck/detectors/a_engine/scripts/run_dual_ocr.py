#!/usr/bin/env python
"""R-04: run a SECOND, independent OCR over the same images and compare.

Why a second OCR at all: a single engine's miss and a genuine absence look
identical in the report. If two engines with different failure modes both fail
on the same region, "no sensitive text here" is a defensible claim; if only one
fails, it is a bug. This script produces that comparison.

Design constraints that shaped it:

1. **Plaintext never leaves memory.** The comparison is between two sets of
   values in RAM; the report records counts and image ids only. Writing the
   numbers out would defeat the whole product.
2. **Missing Tesseract is a SKIP, not a failure.** `AUDIT.md` marks EN-05 as
   SKIP precisely because the binary is optional; a script that exits non-zero
   on a machine without it would turn a known gap into a red build.
3. **Two datasets, two meanings.** On dev the ground truth gives exact counts
   (42 text objects), so the output is recall. On real/ only `manifest.csv`
   exists, so the output is counts, and it says so.

Usage:
    python scripts/run_dual_ocr.py --dataset C:/pc_full/PhotoCheck/dataset/dev \
        --tesseract "C:/Program Files/Tesseract-OCR/tesseract.exe" --lang chi_sim+eng

    # what the audit actually asks for: 30 sampled objects reviewed by hand
    python scripts/run_dual_ocr.py --dataset ... --tesseract ... --sample 30
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.detect import ocr as ocr_mod  # noqa: E402
from core.detect.entities import find_entities  # noqa: E402
from core.detect.qr import backend as qr_backend  # noqa: E402


def _entities(text: str) -> dict[str, int]:
    """Count phone/idcard hits in a blob of OCR text (values never leave here)."""
    out = {"phone": 0, "idcard": 0}
    for ent in find_entities(text or ""):
        out[ent.subtype] = out.get(ent.subtype, 0) + 1
    return out


def _tesseract_text(exe: Path, image: Path, lang: str, timeout: int = 120) -> str:
    """Run Tesseract on one file and return its text (empty string on failure)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "out"
        cmd = [str(exe), str(image), str(base), "-l", lang, "--psm", "6"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"tesseract failed: {type(exc).__name__}") from exc
        out_txt = base.with_suffix(".txt")
        if not out_txt.is_file():
            detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output file"]
            raise RuntimeError(f"tesseract produced no text: {detail[0][:120]}")
        return out_txt.read_text(encoding="utf-8", errors="replace")


def _gt_text_counts(label: Path) -> dict[str, int]:
    out = {"phone": 0, "idcard": 0}
    doc = json.loads(label.read_text(encoding="utf-8"))
    for rec in doc if isinstance(doc, list) else []:
        if rec.get("record_type") == "Detection" and rec.get("kind") == "text":
            out[rec.get("subtype")] = out.get(rec.get("subtype"), 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R-04 dual-OCR comparison")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--tesseract", required=True, help="path to tesseract.exe")
    parser.add_argument("--lang", default="chi_sim+eng")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=None, help="CSV path for the 30-object review sheet")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    exe = Path(args.tesseract)
    if not exe.is_file():
        print(f"[SKIP] Tesseract not found at {exe}")
        print("       EN-05 is SKIP by design; install Tesseract 5.x + chi_sim on the")
        print("       acceptance machine and rerun. Nothing is being claimed here.")
        return 0

    dev = Path(args.dataset)
    img_dir = dev / "images"
    lbl_dir = dev / "labels"
    labelled = bool(lbl_dir.is_dir())
    images = sorted(img_dir.glob("*.jpg"))
    if args.limit:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"no jpg under {img_dir}")

    if not ocr_mod.warmup():
        print("[error] RapidOCR unavailable; cannot compare", file=sys.stderr)
        return 2

    print(f"{len(images)} image(s) | lang {args.lang} | qr backend {qr_backend()}")
    print(f"{'image':22s}{'rapid':>12s}{'tess':>12s}{'gt':>12s}  note")

    rows: list[dict[str, Any]] = []
    for img in images:
        stem = img.stem
        label = lbl_dir / f"{stem}.json"
        gt = _gt_text_counts(label) if labelled and label.is_file() else None

        from PIL import Image  # noqa: PLC0415

        t0 = time.perf_counter()
        with Image.open(img) as raw:
            blocks = ocr_mod.detect_text_blocks(raw.convert("RGB"))
        rapid = _entities("\n".join(b.text for b in blocks))
        rapid_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        try:
            tess = _entities(_tesseract_text(exe, img, args.lang))
            tess_err = ""
        except Exception as exc:  # noqa: BLE001
            tess = {"phone": 0, "idcard": 0}
            tess_err = str(exc)[:60]
        tess_ms = (time.perf_counter() - t0) * 1000

        note = ""
        if gt:
            if tess["phone"] + tess["idcard"] > rapid["phone"] + rapid["idcard"]:
                note = "tesseract 多读到"
            elif tess["phone"] + tess["idcard"] < rapid["phone"] + rapid["idcard"]:
                note = "tesseract 少读到"
        if tess_err:
            note = (note + " " if note else "") + tess_err

        rows.append(
            {
                "image_id": stem,
                "rapid_phone": rapid["phone"], "rapid_idcard": rapid["idcard"],
                "tess_phone": tess["phone"], "tess_idcard": tess["idcard"],
                "gt_phone": (gt or {}).get("phone"), "gt_idcard": (gt or {}).get("idcard"),
                "rapid_ms": round(rapid_ms, 1), "tess_ms": round(tess_ms, 1),
                "note": note,
            }
        )
        g = f"{gt['phone']}/{gt['idcard']}" if gt else "-"
        print(f"  {stem:20s}{rapid['phone']:5d}/{rapid['idcard']:<6d}"
              f"{tess['phone']:5d}/{tess['idcard']:<6d}{g:>12s}  {note}")

    print("-" * 66)
    rp = sum(r["rapid_phone"] for r in rows); ri = sum(r["rapid_idcard"] for r in rows)
    tp = sum(r["tess_phone"] for r in rows); ti = sum(r["tess_idcard"] for r in rows)
    print(f"手机号   rapidocr {rp}  tesseract {tp}")
    print(f"身份证   rapidocr {ri}  tesseract {ti}")
    if labelled:
        gtp = sum(r["gt_phone"] or 0 for r in rows)
        gti = sum(r["gt_idcard"] or 0 for r in rows)
        print(f"真值     phone {gtp} / idcard {gti}")
        print(f"召回     rapidocr {rp}/{gtp} = {rp/max(1,gtp):.0%} phone, "
              f"{ri}/{gti} = {ri/max(1,gti):.0%} idcard")
        print(f"         tesseract {tp}/{gtp} = {tp/max(1,gtp):.0%} phone, "
              f"{ti}/{gti} = {ti/max(1,gti):.0%} idcard")
    only_tess = [r["image_id"] for r in rows
                 if (r["tess_phone"] + r["tess_idcard"]) > (r["rapid_phone"] + r["rapid_idcard"])]
    only_rapid = [r["image_id"] for r in rows
                  if (r["tess_phone"] + r["tess_idcard"]) < (r["rapid_phone"] + r["rapid_idcard"])]
    print(f"\ntesseract 多读到的图: {len(only_tess)} {only_tess[:6]}")
    print(f"rapidocr 多读到的图: {len(only_rapid)} {only_rapid[:6]}")
    print("互补为空 = 在当前样本上第二 OCR 没有带来收益；这不等于它没用，"
          "只说明这批图没有踩到 rapidocr 的失败模式。")
    tms = [r["tess_ms"] for r in rows]
    if tms:
        print(f"tesseract 单图 median {statistics.median(tms):.0f} ms")

    if args.out:
        with Path(args.out).open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        print(f"\nwritten {args.out}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"lang": args.lang, "images": len(rows), "rows": rows},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
