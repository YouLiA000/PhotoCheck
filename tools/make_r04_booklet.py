#!/usr/bin/env python3
"""把 R-04 的 30 个对象排成 A4 纸质判读册（评审 A / B 各一册）。

为什么纸质：R-04 要求**两位评审独立判读**。纸质天然隔离——看不到对方的勾，
也没法在同一个屏幕上边讨论边改。

每个对象同时印遮前正控和遮后结果：遮前图先证明原区域确实有文字，遮后图再用
1:1 分辨率判断「是否还能读出」。只有遮后全黑、没有遮前正控的判读册会被拒绝。

两册内容完全相同，只有封面标识不同；不含 tesseract_status / primary_status / agreement
等系统结论，避免评审被机器判断锚定。

用法：
    python tools/make_r04_booklet.py --objects results/r04-b --out results/r04-b/print
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
A4 = (2480, 3508)          # 300 dpi
MARGIN = 150
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
PER_PAGE = 6
BLANK_PIXEL = 24           # 最大像素与最小像素之差 ≤ 此值视为纯色块


def blank_ratio(crops: Path, rows: list[dict]) -> float:
    """近乎纯色的裁剪图占比。

    R-04 的 `manual_crops` 裁自**已遮盖**图，裁剪范围就是遮盖范围，所以天然是黑块。
    这个比例用来在排版前拦住「印一堆黑条」——评审看纯色块只能全勾 not_readable，
    那份判读在证据上是空的。见 docs/R04判读材料核查_2026-11-16.md。
    """
    flags: list[float] = []
    for row in rows:
        path = crops / f"{int(row['sample_no']):02d}.png"
        if not path.is_file():
            continue
        arr = np.array(Image.open(path).convert("L"))
        flags.append(1.0 if int(arr.max()) - int(arr.min()) <= BLANK_PIXEL else 0.0)
    return (sum(flags) / len(flags)) if flags else 0.0


def font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def cover(reviewer: str) -> Image.Image:
    page = Image.new("L", A4, 255)
    d = ImageDraw.Draw(page)
    f_t, f_h, f_b, f_s = font(88), font(54), font(42), font(34)

    d.text((MARGIN, 260), f"R-04 人工判读册 · 评审 {reviewer}", fill=0, font=f_t)
    d.text((MARGIN, 390), "PhotoCheck · 2026-11-16 · 共 30 个对象", fill=90, font=f_s)

    y = 560
    d.text((MARGIN, y), "只判一件事", fill=0, font=f_h); y += 90
    for line in (
        "先看「遮前正控」，确认原区域有文字；再看「遮后」。",
        "遮后仍能读出原文 → 勾 readable；读不出来 → 勾 not_readable。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_b); y += 70

    y += 50
    d.text((MARGIN, y), "四条纪律", fill=0, font=f_h); y += 90
    for line in (
        "1. 只看图，不要参考任何系统结论——本册也没有给你系统结论。",
        "2. 不要和另一位评审讨论。独立性一旦破坏，这两票就作废。",
        "3. 每格都要勾，不许空着；拿不准也要选一个，并在格内写一句为什么。",
        "4. 遮前图只证明有内容；只能根据遮后图决定 readable / not_readable。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_b); y += 80

    y += 60
    d.text((MARGIN, y), "填完交给谁", fill=0, font=f_h); y += 90
    for line in (
        "交回汇总人，由他把 30 行结果录入 r04-objects.csv 的",
        f"manual_reviewer_{reviewer.lower()} 列，并保留本册原件备查。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_b); y += 70

    y += 90
    d.text((MARGIN, y), "评审签名：________________      日期：____________", fill=0, font=f_b)

    d.text((MARGIN, A4[1] - 220), "PhotoCheck · C 侧 · R-04 判读册", fill=120, font=f_s)
    return page


def _paste_fit(page: Image.Image, image: Image.Image, box: tuple[int, int, int, int]) -> None:
    fitted = image.copy()
    fitted.thumbnail((box[2], box[3]), Image.Resampling.LANCZOS)
    x = box[0] + (box[2] - fitted.width) // 2
    y = box[1] + (box[3] - fitted.height) // 2
    page.paste(fitted, (x, y))


def cell(
    page: Image.Image,
    d: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    no: int,
    subtype: str,
    crop_path: Path,
    control_path: Path,
) -> None:
    x, y, w, h = box
    f_no, f_small, f_tick = font(44), font(30), font(38)

    d.rectangle([x, y, x + w, y + h], outline=170, width=2)
    d.text((x + 24, y + 18), f"{no:02d}", fill=0, font=f_no)
    d.text((x + 130, y + 28), subtype, fill=90, font=f_small)
    d.text((x + 24, y + 82), "遮前正控（只确认有文字）", fill=110, font=f_small)
    with Image.open(control_path) as control:
        _paste_fit(page, control.convert("L"), (x + 24, y + 122, w - 48, 190))
    d.line((x + 24, y + 330, x + w - 24, y + 330), fill=170, width=2)
    d.text((x + 24, y + 350), "遮后（按这张判读）", fill=0, font=f_small)
    with Image.open(crop_path) as crop:
        _paste_fit(page, crop.convert("L"), (x + 24, y + 392, w - 48, 260))

    ty = y + h - 110
    d.rectangle([x + 24, ty, x + 74, ty + 50], outline=0, width=3)
    d.text((x + 92, ty - 2), "readable", fill=0, font=f_tick)
    d.rectangle([x + 460, ty, x + 510, ty + 50], outline=0, width=3)
    d.text((x + 528, ty - 2), "not_readable", fill=0, font=f_tick)


def page_of(
    reviewer: str,
    rows: list[dict],
    crops: Path,
    controls: Path,
    page_no: int,
    total: int,
) -> Image.Image:
    page = Image.new("L", A4, 255)
    d = ImageDraw.Draw(page)
    f_h, f_s = font(46), font(30)

    d.text((MARGIN, 100), f"R-04 判读册 · 评审 {reviewer}（第 {page_no}/{total} 页）",
           fill=0, font=f_h)
    d.text((MARGIN, 170),
           "上方是遮前正控，下方是遮后结果；只按遮后结果勾选。每格必勾。",
           fill=90, font=f_s)

    cell_w = (A4[0] - 2 * MARGIN - 40) // 2
    cell_h = 880
    top = 250
    for i, row in enumerate(rows):
        col, line = i % 2, i // 2
        x = MARGIN + col * (cell_w + 40)
        y = top + line * (cell_h + 30)
        crop = crops / f"{int(row['sample_no']):02d}.png"
        control = controls / f"{int(row['sample_no']):02d}.png"
        cell(page, d, (x, y, cell_w, cell_h), int(row["sample_no"]),
             row["subtype"], crop, control)
    return page


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 R-04 A4 判读册（A/B 两册）")
    ap.add_argument("--objects", required=True, help="r04_second_ocr.py 的输出目录")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--allow-blank-crops",
        action="store_true",
        help="即使裁剪图近乎纯色也生成（用于「漏遮检查」口径，见核查文档）",
    )
    args = ap.parse_args(argv)

    src = Path(args.objects)
    csv_path = src / "r04-objects.csv"
    crops = src / "manual_crops"
    controls = src / "control_crops"
    if not csv_path.is_file() or not crops.is_dir() or not controls.is_dir():
        print(f"[error] 找不到 {csv_path}、{crops} 或 {controls}", file=sys.stderr)
        return 2

    with csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("[error] 对象表为空", file=sys.stderr)
        return 2

    missing = [
        r["sample_no"] for r in rows
        if not (crops / f"{int(r['sample_no']):02d}.png").is_file()
    ]
    if missing:
        print(f"[error] 缺裁剪图：{missing}", file=sys.stderr)
        return 2
    missing_controls = [
        r["sample_no"] for r in rows
        if not (controls / f"{int(r['sample_no']):02d}.png").is_file()
    ]
    if missing_controls:
        print(f"[error] 缺遮前正控图：{missing_controls}", file=sys.stderr)
        return 2

    ratio = blank_ratio(crops, rows)
    control_ratio = blank_ratio(controls, rows)
    if ratio >= 0.9 and control_ratio >= 0.9 and not args.allow_blank_crops:
        print(
            f"[refused] {ratio:.0%} 的裁剪图是纯色块。R-04 的 manual_crops 取自已遮盖图，"
            "裁剪范围就是遮盖范围，印出来评审只能全勾 not_readable，判读没有信息量。\n"
            "  · 要做「漏遮检查」口径：加 --allow-blank-crops 强制生成；\n"
            "  · 要做「能否读出」判读：control_crops 也必须包含可见文字。",
            file=sys.stderr,
        )
        return 3

    out = Path(args.out) if args.out else src / "print"
    out.mkdir(parents=True, exist_ok=True)

    total = (len(rows) + PER_PAGE - 1) // PER_PAGE
    for reviewer in ("A", "B"):
        pages = [cover(reviewer)]
        for i in range(total):
            chunk = rows[i * PER_PAGE:(i + 1) * PER_PAGE]
            pages.append(page_of(reviewer, chunk, crops, controls, i + 1, total))
        pdf = out / f"R04判读册_评审{reviewer}.pdf"
        pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=300.0)
        print(f"[ok] {pdf}  {len(pages)} 页")

    print(f"[ok] 共 {len(rows)} 个对象，每人 {total + 1} 页（含封面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
