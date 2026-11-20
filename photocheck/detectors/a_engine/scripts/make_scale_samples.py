#!/usr/bin/env python
"""Build the controlled-variable samples used by the T_ocr scale experiment.

Why a script instead of shipping the images: four PNGs at 0.36-3.0 MP weigh
2.9 MB, which is bigger than the rest of the delivery package. The samples are
fully described by this file, so they are regenerated on demand instead.

What the experiment needs is a set where ONE variable changes at a time:

  same content, different resolution  -> isolates the pixel cost
  same resolution, different rows     -> isolates the text-volume cost

`--master` builds one high-resolution page and downscales it, so every case has
identical glyphs and identical wording. That is the only way to compare a
0.56 MP page against a 1.75 MP page without the text volume changing too.

Usage:
    python scripts/make_scale_samples.py --out reports/t_ocr_scale
    python scripts/bench_ocr.py reports/t_ocr_scale/same_content_1120x1560.png --repeat 3

Results of the run behind reports/T_ocr_规模效应实测_2026-11-08.md:

    900x620   short  0.56 MP   4 blk    68 chars   1558 ms
    900x620   long   0.56 MP  16 blk   714 chars   5783 ms
    1120x1560 short  1.75 MP   5 blk    85 chars   2624 ms
    1120x1560 long   1.75 MP  20 blk   500 chars   5038 ms

Note the 2.2x inversion: the 0.56 MP dense page is slower than the 1.75 MP sparse
one, which is why normalising by megapixels gives the wrong answer.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# A single row of realistic document text. Long enough that "chars" is a
# meaningful axis, short enough that two rows fit in a 900px-wide page.
LINE = "收货地址：北京市昌平区回龙观东大街 3 号 电话 13800138000 订单号 7801234567890"
SHORT = "签收人：张三  13800138000"

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    # Last resort: PIL's bitmap default cannot scale, but the script still runs.
    return ImageFont.load_default()


def _page(width: int, height: int, text: str, font_px: int, rows: int, gap: float) -> Image.Image:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _font(font_px)
    y = int(height * 0.06)
    for _ in range(rows):
        draw.text((int(width * 0.08), y), text, fill="black", font=font)
        y += int(font_px * gap)
    return img


def _master(width: int = 2240, height: int = 3120, font_px: int = 84) -> Image.Image:
    """One high-resolution page; every `same_content_*` case is a downscale of it."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _font(font_px)
    y = 60
    while y < height - 140:
        draw.text((60, y), LINE, fill="black", font=font)
        y += 130
    return img


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="samples for the T_ocr scale experiment")
    parser.add_argument("--out", default="reports/t_ocr_scale")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Group 1: identical content at four resolutions (pixel axis only).
    master = _master()
    for w, h in ((760, 480), (1120, 1560), (1600, 1200), (2000, 1500)):
        p = out / f"same_content_{w}x{h}.png"
        master.resize((w, h), Image.LANCZOS).save(p)
        print(f"wrote {p}  {w}x{h}  {w * h / 1e6:.2f} MP")

    # Group 2: identical resolution, different text volume (character axis only).
    pairs = [
        ("dense_900x620.png", 900, 620, LINE, 22, 16, 1.55),
        ("sparse_900x620.png", 900, 620, SHORT, 26, 4, 1.8),
        ("dense_1120x1560.png", 1120, 1560, LINE, 43, 20, 1.55),
        ("sparse_1120x1560.png", 1120, 1560, SHORT, 34, 5, 1.8),
    ]
    for name, w, h, text, font_px, rows, gap in pairs:
        p = out / name
        _page(w, h, text, font_px, rows, gap).save(p)
        print(f"wrote {p}  {w}x{h}  {rows} rows of {len(text)} chars")

    print("\nMeasure with: python scripts/bench_ocr.py", out, "--repeat 3")
    print("Record the power state: the same image measured 1644.6 ms on AC and")
    print("2841 ms on battery, a 73% swing that dwarfs the acceptance margin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
