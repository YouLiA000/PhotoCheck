"""How far off is a character-level box estimated from a merged OCR block?

Context: on real photos the label and the value share one block -
`寄件人：李四138-0000-0000` - so redacting "the block" blacks out the phone too.
To redact only the name you need the name's pixel box, and the only cheap way to
get it is to guess where inside the block that character range sits.

Three guesses are compared here:

    uniform    every character takes w/len px (ignores that 中 is wide, 0-9 is narrow)
    weighted   中文字 = 1 unit, ASCII = 0.5 unit
    projection tight columns found from the ink profile, then map text index -> column

Two scoring methods are produced, and only one of them works:

  * cropping the box and OCR-ing the crop - sounds objective, is NOT: a 2-character
    crop is too small for the recogniser, so correct boxes get scored as failures
    (3 of 6 here). Results are still printed, labelled as unreliable.
  * edge-cut ratio - ink on the box's left/right edge columns. Cheap, but it
    disagrees with the eye on some correct boxes (anti-aliasing counts as ink).

So the verdict in the report comes from looking at 3x crops, not from either
number. That is itself a finding: this feature has no cheap automatic judge, which
means the acceptance test needs character-level ground truth - a spec requirement
for the dataset rebuild, not a coding detail.

Usage:
    python scripts/proto_inline_bbox.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# (image, block bbox from OCR cache, block text, name, start index, length)
CASES = [
    ("real-001", [344, 512, 826, 120], "寄件人：李四138-0000-0000", "李四", 4, 2),
    ("real-002", [496, 805, 798, 161], "收件人：张三139-1234-5678", "张三", 4, 2),
    ("real-004", [377, 929, 804, 165], "收件人：张三139-1234-5678收伟人张三139-1234-5678", "张三", 4, 2),
    ("real-010", [159, 1021, 356, 93], "收件人：张三", "张三", 4, 2),
]


def est_uniform(bbox, text, start, length):
    x, y, w, h = bbox
    cw = w / len(text)
    return (round(x + start * cw), y, round(length * cw), h)


def est_weighted(bbox, text, start, length):
    x, y, w, h = bbox
    weights = [1.0 if ord(ch) > 127 else 0.5 for ch in text]
    unit = w / sum(weights)
    off = sum(weights[:start]) * unit
    return (round(x + off), y, round(sum(weights[start:start + length]) * unit), h)


def est_projection(image: Image.Image, bbox, text, start, length):
    """Split the block into character columns by ink profile, then map index -> cell."""
    x, y, w, h = bbox
    gray = np.asarray(image.convert("L").crop((x, y, x + w, y + h)))
    ink = (gray < 160).sum(axis=0)          # per-column ink count
    thresh = max(1, int(ink.max() * 0.04))

    cells, run = [], None
    for i, v in enumerate(ink):
        if v > thresh and run is None:
            run = i
        elif v <= thresh and run is not None:
            if i - run >= 2:
                cells.append((run, i))
            run = None
    if run is not None:
        cells.append((run, len(ink)))

    if len(cells) != len(text):
        return None, len(cells)      # cannot map 1:1; caller falls back

    c0, c1 = cells[start], cells[start + length - 1]
    return (x + c0[0], y, c1[1] - c0[0], h), len(cells)


def ocr_text(crop: Image.Image, ocr_mod) -> str:
    try:
        blocks = ocr_mod.detect_text_blocks(crop)
    except Exception:
        return ""
    return "".join(b.text for b in blocks).strip()


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    import sys

    sys.path.insert(0, str(here))
    from core.detect import ocr as ocr_mod

    ap = argparse.ArgumentParser(description="Compare inline name-box estimators")
    ap.add_argument("--real-dir", default="C:/pc_full/PhotoCheck/dataset/real")
    ap.add_argument("--out", default=str(here / "reports" / "proto_inline_bbox"))
    args = ap.parse_args(argv)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    ocr_mod.warmup()

    results = []
    tiles = []
    for image_id, bbox, text, name, start, length in CASES:
        img = Image.open(Path(args.real_dir) / f"{image_id}.jpg").convert("RGB")
        row = {"image": image_id, "text": text, "name": name}
        for label, fn in (("uniform", est_uniform), ("weighted", est_weighted)):
            box = fn(bbox, text, start, length)
            crop = img.crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
            got = ocr_text(crop, ocr_mod)
            row[label] = {"box": box, "ocr": got, "ok": got == name}
            tiles.append((f"{image_id} {label}", crop, got == name))
        pbox, ncells = est_projection(img, bbox, text, start, length)
        if pbox is None:
            row["projection"] = {"box": None, "ocr": "", "ok": False, "note": f"{ncells} cells vs {len(text)} chars"}
            tiles.append((f"{image_id} projection", img.crop((bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])), False))
        else:
            crop = img.crop((pbox[0], pbox[1], pbox[0] + pbox[2], pbox[1] + pbox[3]))
            got = ocr_text(crop, ocr_mod)
            row["projection"] = {"box": pbox, "ocr": got, "ok": got == name, "note": f"{ncells} cells"}
            tiles.append((f"{image_id} projection", crop, got == name))
        results.append(row)

    # --- print ---
    for r in results:
        print(f"\n{r['image']}  {r['text'][:34]!r}  期望={r['name']}")
        for k in ("uniform", "weighted", "projection"):
            d = r[k]
            mark = "OK  " if d["ok"] else "FAIL"
            print(f"   {k:11s} {mark} box={d['box']}  ocr={d['ocr']!r}  {d.get('note','')}")

    hits = {k: sum(1 for r in results if r[k]["ok"]) for k in ("uniform", "weighted", "projection")}
    print("\n=== OCR-of-crop 命中数 / 4 ===")
    for k, v in hits.items():
        print(f"  {k:11s} {v}/4")
    print("\n  !! 这个判据不可信，别把它当结论。裁 2 个字的图去 OCR，"
          "真实通过率是 3/6 被误判为失败：")
    print("     real-010 三个方案框都对，OCR 只读回 '张'；real-002 weighted 框对，OCR 读回空。")
    print("     正确判据是 3x 放大看图（estimates_sheet.png / zoom_small.png），"
          "见 A_字段级bbox验证_2026-11-14.md。")

    # --- contact sheet ---
    CW, CH, COLS = 460, 84, 2
    rows_n = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CW, rows_n * CH + 34), "white")
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 17)
    d.text((8, 7), "crop of each estimated name box (green border = OCR returned exactly the name)", fill="black", font=font)
    for i, (label, crop, ok) in enumerate(tiles):
        cx, cy = (i % COLS) * CW, 34 + (i // COLS) * CH
        scale = min((CW - 20) / max(1, crop.width), (CH - 30) / max(1, crop.height), 2.0)
        c = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
        sheet.paste(c, (cx + 10, cy + 20))
        d.rectangle([cx + 2, cy + 2, cx + CW - 3, cy + CH - 3],
                    outline="#16a34a" if ok else "#dc2626", width=2)
        d.text((cx + 10, cy + 2), label, fill="#111111", font=font)
    path = outdir / "estimates_sheet.png"
    sheet.save(path)
    print(f"\n拼图 -> {path}")

    (outdir / "results.json").write_text(
        json.dumps({"cases": results, "hits": hits}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"数据 -> {outdir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
