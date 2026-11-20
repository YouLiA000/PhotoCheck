#!/usr/bin/env python
"""Build the layer-3 review pack: side-by-side images plus a blank judgement sheet.

Layer 3 asks a question code cannot answer - "did the redaction cover too much,
swallowing the name/address next to the phone number". `AUDIT.md` marks it FAIL
because no judgement sheet exists at all, not because it was judged and failed.

What this produces, so two people can judge independently on paper:

    reviews/<image_id>.png   left = original, right = exported, same scale
    reviews/判读表.csv        one row per image, judgement columns left EMPTY
    reviews/summary.json     machine numbers, for cross-checking the humans

Two numbers come from the images themselves and are worth reading before the
human does: the fraction of the page changed by redaction, and how many text
blocks A can still read *outside* the redacted areas. A page where 60% of the
pixels changed is suspicious no matter what the boxes were supposed to be.

The sheet ships with empty 判读人 / 结论 / 备注 columns on purpose: filling them
in by script would be exactly the fake-pass the audit is complaining about.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def _load_pillow():
    from PIL import Image, ImageChops, ImageDraw  # noqa: PLC0415

    return Image, ImageChops, ImageDraw


def _changed_ratio(orig, exported, threshold: int = 8) -> tuple[float, tuple[int, int, int, int] | None]:
    """Fraction of pixels redaction actually changed, plus their bounding box.

    A per-pixel difference is the only honest way to measure "how much got
    covered": the plan says which boxes were requested, the export says which
    pixels moved. They are not always the same set.
    """
    Image, ImageChops, _ = _load_pillow()
    if orig.size != exported.size:
        return 0.0, None
    diff = ImageChops.difference(orig.convert("RGB"), exported.convert("RGB"))
    gray = diff.convert("L").point(lambda v: 255 if v > threshold else 0)
    hist = gray.histogram()
    changed = hist[255]
    total = orig.size[0] * orig.size[1]
    bbox = gray.getbbox()
    return (changed / total if total else 0.0), bbox


def _side_by_side(orig, exported, label_l: str, label_r: str, max_height: int = 900):
    Image, _, ImageDraw = _load_pillow()
    scale = min(1.0, max_height / max(orig.height, exported.height))
    w = int(orig.width * scale)
    h = int(orig.height * scale)
    left = orig.convert("RGB").resize((w, h), Image.LANCZOS)
    right = exported.convert("RGB").resize((w, h), Image.LANCZOS)
    pad, bar = 12, 28
    canvas = Image.new("RGB", (w * 2 + pad * 3, h + bar + pad * 2), "white")
    canvas.paste(left, (pad, bar + pad))
    canvas.paste(right, (w + pad * 2, bar + pad))
    draw = ImageDraw.Draw(canvas)
    draw.text((pad + 4, 8), label_l, fill="black")
    draw.text((w + pad * 2 + 4, 8), label_r, fill="black")
    return canvas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="layer-3 review pack")
    parser.add_argument("--layer2", required=True, help="JSON produced by eval_layer2_export.py")
    parser.add_argument("--dataset", required=True, help="path to dataset/dev (for the original image)")
    parser.add_argument("--out", default="reports/layer3_review")
    parser.add_argument("--max-images", type=int, default=0)
    args = parser.parse_args(argv)

    doc = json.loads(Path(args.layer2).read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = doc.get("rows") or []
    if args.max_images:
        rows = rows[: args.max_images]
    if not rows:
        raise SystemExit("layer-2 report has no rows; run eval_layer2_export.py first")

    dev = Path(args.dataset)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for row in rows:
        image_id = row["image_id"]
        exported = Path(row["output"])
        src = dev / "images" / f"{image_id}.jpg"
        if not src.is_file():
            cands = sorted((dev / "images").glob(f"{image_id}.*"))
            if not cands:
                continue
            src = cands[0]
        if not exported.is_file():
            continue

        Image, _, _ = _load_pillow()
        with Image.open(src) as a, Image.open(exported) as b:
            orig, out_img = a.convert("RGB"), b.convert("RGB")
            ratio, bbox = _changed_ratio(orig, out_img)
            side = _side_by_side(orig, out_img, f"原图 {image_id}", "导出（已脱敏）")
            side.save(out_dir / f"{image_id}.png")

        entries.append(
            {
                "image_id": image_id,
                "detections": row.get("detected"),
                "redacted_regions": row.get("selected"),
                "meta_count": row.get("meta_count"),
                "disposal_state": row.get("disposal_state"),
                "residual_count": row.get("residual_count"),
                "changed_pixel_ratio": round(ratio, 4),
            # Re-encoding a JPEG moves a few pixels even with nothing redacted
            # (measured 0.01-0.04% on the clean images here). Below this floor the
            # difference is compression noise, not coverage - saying "0.03% was
            # redacted" would send a human hunting for a box that does not exist.
            "change_kind": "none" if ratio < 0.005 else "redacted",
                "changed_bbox": list(bbox) if bbox else None,
                "compare_image": f"{image_id}.png",
            }
        )

    sheet = out_dir / "判读表.csv"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["image_id", "检出项", "遮盖区域数", "像素改动占比", "改动性质", "导出残留", "处置状态",
             "对比图", "判读人A_过遮/合适/欠遮", "判读人A_说明",
             "判读人B_过遮/合适/欠遮", "判读人B_说明", "是否需重做"]
        )
        for e in entries:
            w.writerow(
                [e["image_id"], e["detections"], e["redacted_regions"],
                 f"{e['changed_pixel_ratio']:.2%}", e["change_kind"],
                 e["residual_count"], e["disposal_state"],
                 e["compare_image"], "", "", "", "", ""]
            )

    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "layer": 3,
                "definition": "human judgement: is the redaction wider than the sensitive content",
                "source_layer2": str(args.layer2),
                "images": len(entries),
                "note": (
                    "判读列故意留空。脚本填上就是 AUDIT 说的那种假通过。"
                    "两人独立填完再比对；不一致的进复判。"
                    "改动性质 none = 低于 0.5%，是 JPEG 重编码噪声，不是遮盖。"
                ),
                "entries": entries,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    ratios = sorted(e["changed_pixel_ratio"] for e in entries)
    print(f"{len(entries)} review page(s) -> {out_dir}")
    print(f"  {sheet.name}: 判读列留空，两人独立填")
    if ratios:
        mid = ratios[len(ratios) // 2]
        print(f"  像素改动占比: median {mid:.2%}, max {ratios[-1]:.2%}, min {ratios[0]:.2%}")
        heavy = [e for e in entries if e["changed_pixel_ratio"] > 0.5]
        if heavy:
            print(f"  改动超过半张图的有 {len(heavy)} 张，优先看："
                  + ", ".join(e['image_id'] for e in heavy[:6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
