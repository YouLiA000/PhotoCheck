#!/usr/bin/env python
"""K-03 / W2 must-do: measure the T_ocr baseline.

The K-03 performance section fixes the procedure: **no fixed number is promised**.
W2 measures the baseline T_ocr first, and only then does the W4 acceptance line
become "per image <= T_ocr x 1.2". This script is what produces that number.

Outputs:
  1. Console table: per-image timing plus global percentiles
  2. reports/t_ocr_baseline_<date>.json: quotable by README / closing report
  3. Prints the W4 acceptance line, computed from the FROZEN baseline
     (FROZEN_BASELINE_T_OCR_MS x 1.2), not from whatever this run measured

Privacy: the report holds **file name, size, timing, block count** only - never any
recognised text.

Usage:
    python scripts/bench_ocr.py ../../dataset/dev --limit 20 --repeat 3
    python scripts/bench_ocr.py a.jpg b.jpg --repeat 5
    python scripts/bench_ocr.py dataset/dev --single-scale   # timing of the fallback path
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import OcrConfig  # noqa: E402
from core.detect import ocr as ocr_mod  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"}

# Frozen T_ocr baseline, in ms.
#
# The W4 acceptance line has to be a FIXED number. If it were recomputed from
# whatever this run measured, then adding a batch of small images would lower
# "T_ocr", lower the acceptance line with it, and the project would quietly
# grade itself against a softer bar - every re-measurement would be a chance to
# relax the target.
#
# 1686.3 is the conservative figure from 2026-11-06 (a same-day re-run gave
# 1640.5; the slower number is kept on purpose). Re-measuring is still useful:
# it tells you whether this machine got slower, which is exactly what the
# frozen baseline is compared against. Moving the baseline is a deliberate
# decision that has to be written down, not a side effect of running the script.
FROZEN_BASELINE_T_OCR_MS = 1686.3

# W4 acceptance multiplier from the K-03 section of the spec card.
W4_MULTIPLIER = 1.2
P95_MULTIPLIER = 1.5   # B 2026-11-15 proposal, adopted


def collect_files(inputs: list[str], limit: int) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES))
        elif path.is_file():
            files.append(path)
        else:
            print(f"[warn] skipping a path that does not exist: {path}", file=sys.stderr)
    return files[:limit] if limit and limit > 0 else files


def _host_state() -> dict:
    """CPU model and whether the machine runs on battery.

    These two are not decorations. The same image measured 1644.6 ms on 2026-11-07
    and 2841.4 ms on 2026-11-08, with no code change, no CPU downclock and no other
    process: the laptop was on battery. A timing report without them is not
    reproducible, and a threshold compared across them measures the power supply.
    """
    cpu = ""
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:
            cpu = platform.processor() or "unknown"
    else:
        cpu = platform.processor() or platform.machine() or "unknown"

    power = "unknown"
    if sys.platform == "win32":
        try:
            import ctypes

            class _SPS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("Reserved1", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            st = _SPS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                power = {0: "battery", 1: "ac", 255: "unknown"}.get(st.ACLineStatus, "unknown")
                if power == "battery":
                    power = f"battery({st.BatteryLifePercent}%)"
        except Exception:
            pass

    return {"cpu": cpu, "power": power}


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[idx]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="K-03 T_ocr baseline measurement (W2)",
        # Abbreviations are off so that a typo like `--out path.json` fails loudly
        # instead of being silently read as `--out-dir path.json` and creating a
        # directory with the report name.
        allow_abbrev=False,
    )
    parser.add_argument("inputs", nargs="+", help="image directory or image files")
    parser.add_argument("--limit", type=int, default=0, help="max images to measure (0 = no limit)")
    parser.add_argument("--repeat", type=int, default=3, help="repeats per image, min is kept")
    parser.add_argument("--single-scale", dest="dual_scale", action="store_false", default=True,
                        help="disable dual-scale (measures the fallback path)")
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args(argv)

    if not ocr_mod.warmup():
        print("[error] RapidOCR unavailable (dependency missing). Degraded: card unusable, README must say so.",
              file=sys.stderr)
        return 3

    files = collect_files(args.inputs, args.limit)
    if not files:
        print("[error] no images found", file=sys.stderr)
        return 2

    from PIL import Image, ImageOps

    cfg = OcrConfig(dual_scale=args.dual_scale)
    rows: list[dict[str, object]] = []
    print(f"{len(files)} image(s), {max(1, args.repeat)} repeat(s) each (min kept, jitter removed)\n")
    header = f"{'file':<28}{'size':>12}{'blocks':>8}{'chars':>8}{'min(ms)':>10}{'mean(ms)':>10}"
    print(header)
    print("-" * len(header))

    for path in files:
        try:
            with Image.open(path) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] cannot open {path.name}: {type(exc).__name__}", file=sys.stderr)
            continue

        ocr_mod.detect_text_blocks(image, cfg=cfg)  # one warmup pass, not timed
        samples: list[float] = []
        blocks = 0
        chars = 0
        for _ in range(max(1, args.repeat)):
            started = time.perf_counter()
            found = ocr_mod.detect_text_blocks(image, cfg=cfg)
            blocks = len(found)
            chars = sum(len(b.text) for b in found)
            samples.append((time.perf_counter() - started) * 1000.0)

        row = {
            "file": path.name,
            "width": image.width,
            "height": image.height,
            "blocks": blocks,
            # Character count is the real cost driver, not pixels: a 0.56 MP page
            # of dense text measured 2.2x slower than a 1.75 MP page of sparse
            # text. Recorded so two runs are never compared across different
            # text volumes without noticing.
            "chars": chars,
            "min_ms": round(min(samples), 1),
            "mean_ms": round(statistics.fmean(samples), 1),
        }
        rows.append(row)
        size = f"{row['width']}x{row['height']}"
        print(f"{str(row['file'])[:27]:<28}{size:>12}{blocks:>8}{chars:>8}"
              f"{row['min_ms']:>10}{row['mean_ms']:>10}")

    if not rows:
        print("[error] not a single image completed", file=sys.stderr)
        return 4

    mins = [float(r["min_ms"]) for r in rows]
    t_ocr = statistics.median(mins)
    target = FROZEN_BASELINE_T_OCR_MS * W4_MULTIPLIER

    try:
        import rapidocr_onnxruntime

        rapidocr_version = getattr(rapidocr_onnxruntime, "__version__", "unknown")
    except Exception:
        rapidocr_version = "unknown"

    mps = [float(r["width"]) * float(r["height"]) / 1e6 for r in rows]
    char_counts = [int(r.get("chars") or 0) for r in rows]
    summary = {
        "measured_t_ocr_ms": round(t_ocr, 1),
        # T_ocr is only comparable between runs with similar sample shape. Record
        # what this batch looked like, because "1686 ms" and "5513 ms" were both
        # measured with this script and differ 3.2x purely on sample content.
        "median_megapixels": round(statistics.median(mps), 2),
        "median_chars": int(statistics.median(char_counts)) if char_counts else 0,
        "ms_per_kchar": round(t_ocr / max(1, statistics.median(char_counts)) * 1000, 1)
        if char_counts and statistics.median(char_counts)
        else None,
        "baseline_t_ocr_ms": FROZEN_BASELINE_T_OCR_MS,
        "w4_target_ms": round(target, 1),
        # Kept only as a diagnostic: it answers "how far off is this batch from
        # the frozen baseline". It is NOT the acceptance line.
        "w4_target_if_remeasured_ms": round(t_ocr * W4_MULTIPLIER, 1),
        "samples": len(rows),
        "median_min_ms": round(statistics.median(mins), 1),
        "mean_min_ms": round(statistics.fmean(mins), 1),
        "p95_min_ms": round(_percentile(mins, 0.95), 1),
        "max_min_ms": round(max(mins), 1),
        "dual_scale": bool(args.dual_scale),
        "host": _host_state(),
    }

    print("\n" + "=" * 46)
    print(f"T_ocr measured (median) = {summary['measured_t_ocr_ms']} ms  (n={summary['samples']} images)")
    print(f"p95                     = {summary['p95_min_ms']} ms")
    print(f"W4 acceptance (frozen)  = {summary['baseline_t_ocr_ms']} x 1.2 = {summary['w4_target_ms']} ms")
    print(f"  baseline is frozen; this run alone would have implied "
          f"{summary['w4_target_if_remeasured_ms']} ms, which is NOT used")
    worst = summary["max_min_ms"]
    # B's proposal (2026-11-15) adds a p95 gate at 1.5x and leaves max un-gated but
    # reported. Adopted so this script and the spec cannot drift apart again.
    p95_line = FROZEN_BASELINE_T_OCR_MS * P95_MULTIPLIER
    p95 = summary["p95_min_ms"]
    print(f"p95 gate (<= 1.5x baseline = {round(p95_line, 1)} ms): "
          f"{'PASS' if p95 <= p95_line else 'FAIL'}  (measured {p95} ms)")
    print(f"median gate (<= 1.2x baseline = {round(target, 1)} ms): "
          f"{'PASS' if t_ocr <= target else 'FAIL'}  (measured {round(t_ocr, 1)} ms)")
    if worst > summary["w4_target_ms"]:
        print(f"  WARNING: slowest image {worst} ms already exceeds the acceptance line")
    else:
        print(f"  slowest image {worst} ms, margin {round(target - worst, 1)} ms")
    if worst > 2 * FROZEN_BASELINE_T_OCR_MS:
        print(f"  NOTICE: slowest image is {worst / FROZEN_BASELINE_T_OCR_MS:.1f}x baseline; "
              f"explain that image complexity (dense text vs sparse)")
    host = summary["host"]
    print(f"  host: {host['cpu']} | power: {host['power']}")
    if host["power"].startswith("battery"):
        print("  WARNING: on battery. These timings are NOT comparable with an AC run; "
              "re-measure on AC before quoting pass/fail against the frozen baseline.")
    print(f"  this batch: median {summary['median_megapixels']} MP / "
          f"{summary['median_chars']} chars -> {summary['ms_per_kchar']} ms/kchar")
    print("  T_ocr is NOT comparable across batches of different text volume;")
    print("  see reports/T_ocr_规模效应实测_2026-11-08.md")
    print("=" * 46)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"t_ocr_baseline_{datetime.now():%Y%m%d_%H%M}.json"
    payload = {
        "card": "K-03",
        "note": "只含文件名/尺寸/耗时，不含任何识别文字",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "rapidocr": rapidocr_version,
            **_host_state(),
        },
        "config": {
            "min_score": cfg.min_score,
            "long_side": cfg.long_side,
            "dual_scale": cfg.dual_scale,
            "upscale": cfg.upscale,
        },
        "summary": summary,
        "rows": rows,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out_path}")
    print("Quote measured_t_ocr_ms as the current machine speed; the acceptance line stays")
    print("w4_target_ms (frozen baseline). Changing the baseline needs a written decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
