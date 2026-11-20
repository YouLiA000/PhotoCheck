#!/usr/bin/env python
"""Evidence for the 1D barcode path: one EAN-13 positive, two negatives.

Why this script exists: `detector_status()['qr_backend'] == 'pyzbar'` only proves
the decoder loaded. It says nothing about whether a barcode is actually found,
boxed and labelled `qr_code`. Reviewers asked for the difference, so this script
produces all four pieces of evidence in one run:

  1. a synthetic EAN-13 (check digit computed, 95 modules, 4 px per module)
  2. a look-alike with a deliberately wrong check digit - same rhythm, invalid
  3. decorative stripes - vertical bars with no start/stop guards
  4. the Detection records for each: subtype, risk, bbox, decodable

Result as of 2026-11-07 on this host:

    ean13_good      1 x qr_code  risk 1  bbox [120, 70, 380, 119]
    ean13_badcheck  0
    stripes         0

The two negatives are the important half. A detector that reports every striped
region as a barcode would pass the positive test and still be useless.

`--fix-crt DIR` exists because this host is missing the 64-bit MSVCR120.dll that
libzbar links against; pointing the DLL search at a directory that has it makes
pyzbar load. That is a host repair, NOT a product dependency - the shipped
detector does not do this, it just reports `qr_backend='cv2'` when pyzbar is
unavailable.

Usage:
    python scripts/verify_barcode.py
    python scripts/verify_barcode.py --fix-crt "C:/Program Files/Microsoft Office/root/Client"
    python scripts/verify_barcode.py --out-dir reports/barcode_evidence
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# EAN-13 tables. Kept local so the script needs no barcode library.
_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
      "0110001", "0101111", "0111011", "0110111", "0001011"]
_G = ["0100111", "0110011", "0011011", "0100001", "0011101",
      "0111001", "0000101", "0010001", "0001001", "0010111"]
_R = ["1110010", "1100110", "1101100", "1000010", "1011100",
      "1001110", "1010000", "1000100", "1001000", "1110100"]
_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
           "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean13(body12: str) -> str:
    """Append the EAN-13 check digit to a 12-digit body."""
    if len(body12) != 12 or not body12.isdigit():
        raise ValueError("body must be 12 digits")
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def ean13_bits(code13: str) -> str:
    first, rest = code13[0], code13[1:]
    out = "101"
    for i, digit in enumerate(rest[:6]):
        out += (_L if _PARITY[int(first)][i] == "L" else _G)[int(digit)]
    out += "01010"
    for digit in rest[6:]:
        out += _R[int(digit)]
    return out + "101"


def render(code13: str, scale: int = 4, height: int = 120, quiet: int = 20):
    """Black bars on white, with a quiet zone - the shape a real label has."""
    import numpy as np

    bits = ean13_bits(code13)
    width = (quiet * 2 + len(bits)) * scale
    img = np.full((height, width), 255, np.uint8)
    for i, bit in enumerate(bits):
        if bit == "1":
            x0 = (quiet + i) * scale
            img[:, x0:x0 + scale] = 0
    return img


def pad(img, canvas=(260, 620)):
    """Centre on a larger sheet so the detector is not fed an edge-to-edge crop."""
    import numpy as np

    h, w = img.shape
    big = np.full(canvas, 255, np.uint8)
    top, left = (canvas[0] - h) // 2, (canvas[1] - w) // 2
    big[top:top + h, left:left + w] = img
    return big


def decorative_stripes(canvas=(260, 620), period: int = 6, bar: int = 3):
    """Same bar rhythm as a barcode, but no start/stop guards - a real negative."""
    import numpy as np

    img = np.full(canvas, 255, np.uint8)
    for x in range(0, canvas[1], period):
        img[:, x:x + bar] = 0
    img[:50, :] = 255
    img[-50:, :] = 255
    return img


def _try_fix_crt(directory: str | None) -> str | None:
    """Make pyzbar importable when the host lacks MSVCR120; returns a note."""
    if not directory:
        return None
    if not os.path.isdir(directory):
        return f"--fix-crt {directory!r} is not a directory, ignored"
    try:
        os.add_dll_directory(directory)
        return f"added DLL directory {directory!r} (host repair, not a product dependency)"
    except Exception as exc:  # noqa: BLE001
        return f"could not add {directory!r}: {type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1D barcode evidence (R-05)")
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "barcode_evidence"))
    parser.add_argument("--fix-crt", default=None, help="directory containing 64-bit MSVCR120.dll")
    parser.add_argument("--code", default="690123456789", help="12-digit EAN-13 body")
    args = parser.parse_args(argv)

    note = _try_fix_crt(args.fix_crt)

    from PIL import Image

    from core.detect import qr as qr_mod

    try:
        import pyzbar.pyzbar  # noqa: F401

        pyzbar_ok = True
        pyzbar_error = None
    except Exception as exc:  # noqa: BLE001
        pyzbar_ok = False
        pyzbar_error = f"{type(exc).__name__}: {exc}"

    if not pyzbar_ok:
        print("[error] pyzbar cannot load, so the 1D path is unreachable on this host.")
        print(f"        reason: {pyzbar_error}")
        print("        Install the 64-bit Visual C++ 2013 runtime, or rerun with")
        print("        --fix-crt <dir containing MSVCR120.dll> to verify anyway.")
        print("        Technical detail: libzbar-64.dll links MSVCR120.dll; see BC-0008.")
        return 3

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    code = ean13(args.code)
    bad_code = code[:-1] + str((int(code[-1]) + 5) % 10)

    cases = [
        ("ean13_good", pad(render(code)), "positive: valid check digit"),
        ("ean13_badcheck", pad(render(bad_code)), "negative: look-alike, wrong check digit"),
        ("stripes", decorative_stripes(), "negative: decorative stripes, no guards"),
    ]

    results = []
    for name, arr, label in cases:
        png = out_dir / f"{name}.png"
        # PIL, not `cv2.imwrite`: the latter returns False (no exception) when the
        # path contains non-ASCII characters, and this project lives under a
        # Chinese directory name. That silent failure cost a debugging round.
        Image.fromarray(arr).save(png)
        image = Image.open(png).convert("RGB")
        hits = qr_mod.detect_qr(image)
        records = [d.to_dict() for d in qr_mod.build_detections(hits, name)]
        results.append(
            {
                "case": name,
                "label": label,
                "image": png.name,
                "size": list(arr.shape[::-1]),
                "hit_count": len(hits),
                "detections": [
                    {
                        "subtype": r["subtype"],
                        "risk": r["risk"],
                        "bbox": r["bbox"],
                        "decodable": r["details"].get("decodable"),
                    }
                    for r in records
                ],
            }
        )
        detail = ", ".join(
            f"{r['subtype']}/risk {r['risk']}/bbox {r['bbox']}" for r in records
        ) or "-"
        print(f"  {name:16s} hits={len(hits)}  {detail}")

    ok = (
        results[0]["hit_count"] >= 1
        and results[0]["detections"][0]["subtype"] == "qr_code"
        and results[1]["hit_count"] == 0
        and results[2]["hit_count"] == 0
    )

    payload = {
        "card": "K-05 (1D barcode half)",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backend": qr_mod.backend(),
        "pyzbar_ok": pyzbar_ok,
        "host_note": note,
        "ean13": code,
        "modules": len(ean13_bits(code)),
        "scale_px_per_module": 4,
        "expectation": "positive -> 1 x qr_code; both negatives -> 0",
        "passed": ok,
        "cases": results,
    }
    out_json = out_dir / "results.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"backend={qr_mod.backend()}  expectation met={ok}")
    print(f"written {out_json}")
    if note:
        print(f"note: {note}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
