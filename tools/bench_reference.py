#!/usr/bin/env python3
"""Measure and validate the frozen K-03 OCR reference set."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REFERENCE_ROOT = ROOT / "dataset" / "k03_ref_v1"
REQUIRED_REPORT_FIELDS = {
    "version",
    "reference_manifest_sha256",
    "host",
    "config",
    "summary",
    "rows",
}
REQUIRED_SUMMARY_FIELDS = {
    "median_of_image_medians_ms",
    "p95_of_image_medians_ms",
    "max_of_image_medians_ms",
    "n_images",
    "median_megapixels",
    "median_ocr_chars",
    "p95_bootstrap_ci_ms",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _host_state() -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or cpu
    except (ImportError, OSError):
        pass
    power = "unknown"
    try:
        status = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            # SYSTEM_POWER_STATUS is four bytes; the first is ACLineStatus.
            raw = int(status.value).to_bytes(4, "little", signed=False)
            power = {0: "battery", 1: "ac", 255: "unknown"}.get(raw[0], "unknown")
    except (AttributeError, OSError, ValueError):
        pass
    return {"cpu": cpu, "power": power, "platform": platform.platform(), "python": platform.python_version()}


def _load_manifest(reference_root: Path) -> dict[str, Any]:
    manifest_path = reference_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != "k03_ref_v1":
        raise ValueError("unexpected K-03 reference version")
    if len(manifest.get("items") or []) != 5:
        raise ValueError("k03_ref_v1 must contain exactly five images")
    for item in manifest["items"]:
        path = reference_root / item["file"]
        if _sha256(path) != item["sha256"]:
            raise ValueError(f"reference hash mismatch: {path}")
    return manifest


def _median_ms(image_path: Path, runs: int) -> tuple[float, int, float]:
    from photocheck.detectors.a_engine.core.detect import ocr

    image = Image.open(image_path).convert("RGB")
    ocr.detect_text_blocks(image)  # explicit discard
    samples = []
    blocks = []
    for _ in range(runs):
        started = time.perf_counter()
        blocks = ocr.detect_text_blocks(image)
        samples.append((time.perf_counter() - started) * 1000)
    chars = sum(len(block.text) for block in blocks)
    width, height = image.size
    return statistics.median(samples), chars, width * height / 1_000_000


def validate_report(report: dict[str, Any]) -> None:
    missing = REQUIRED_REPORT_FIELDS - set(report)
    if missing:
        raise ValueError(f"report missing fields: {sorted(missing)}")
    missing = REQUIRED_SUMMARY_FIELDS - set(report.get("summary") or {})
    if missing:
        raise ValueError(f"summary missing fields: {sorted(missing)}")
    summary = report["summary"]
    if summary["n_images"] < 5:
        raise ValueError("reference report must contain at least five images")
    if summary["p95_of_image_medians_ms"] is None:
        raise ValueError("p95_of_image_medians_ms is required")
    if report["host"].get("power") != "ac" and not report.get("diagnostic_battery"):
        raise ValueError("formal K-03 measurement requires AC power")


def measure(
    reference_root: Path = REFERENCE_ROOT,
    *,
    runs: int = 3,
    bootstrap: int = 2000,
    seed: int = 7,
) -> dict[str, Any]:
    if runs < 3:
        raise ValueError("runs must be at least 3")
    manifest = _load_manifest(reference_root)
    from photocheck.detectors.a_engine.core.detect import ocr

    if not ocr.warmup():
        raise RuntimeError("RapidOCR is unavailable")
    rows = []
    image_medians = []
    megapixels = []
    char_counts = []
    for item in manifest["items"]:
        median, chars, mp = _median_ms(reference_root / item["file"], runs)
        image_medians.append(median)
        megapixels.append(mp)
        char_counts.append(chars)
        rows.append(
            {
                "id": item["id"],
                "file": item["file"],
                "width": item["width"],
                "height": item["height"],
                "drawn_chars": item["drawn_chars"],
                "ocr_chars": chars,
                "image_median_ms": round(median, 1),
            }
        )

    values = np.asarray(image_medians, dtype=float)
    p95 = float(np.percentile(values, 95, method="linear"))
    rng = np.random.default_rng(seed)
    boot = np.asarray(
        [
            np.percentile(rng.choice(values, size=len(values), replace=True), 95, method="linear")
            for _ in range(bootstrap)
        ]
    )
    report = {
        "version": "k03_ref_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reference_manifest_sha256": _sha256(reference_root / "manifest.json"),
        "host": _host_state(),
        "config": {
            "runs_per_image": runs,
            "discarded_runs_per_image": 1,
            "statistical_unit": "per-image median",
            "long_side": 1600,
            "dual_scale": True,
        },
        "summary": {
            "median_of_image_medians_ms": round(float(np.median(values)), 1),
            "p95_of_image_medians_ms": round(p95, 1),
            "max_of_image_medians_ms": round(float(values.max()), 1),
            "n_images": len(values),
            "median_megapixels": round(float(np.median(megapixels)), 3),
            "median_ocr_chars": round(float(np.median(char_counts)), 1),
            "p95_bootstrap_ci_ms": [
                round(float(np.percentile(boot, 2.5)), 1),
                round(float(np.percentile(boot, 97.5)), 1),
            ],
            "bootstrap_runs": bootstrap,
            "bootstrap_seed": seed,
        },
        "rows": rows,
    }
    validate_report(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure frozen K-03 reference set")
    parser.add_argument("--reference", default=str(REFERENCE_ROOT))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=None)
    parser.add_argument("--diagnostic-battery", action="store_true")
    args = parser.parse_args(argv)
    report = measure(
        Path(args.reference),
        runs=args.runs,
        bootstrap=args.bootstrap,
        seed=args.seed,
    )
    if args.diagnostic_battery:
        report["diagnostic_battery"] = True
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
