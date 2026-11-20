#!/usr/bin/env python3
"""把二维码探针素材排成可直接打印的 A4 PDF（300 dpi）。

为什么单独做打印页，而不是直接打印 contact_sheet.png：

- `contact_sheet.png` 是屏幕用的宽幅图，码只有 200px，打出来边长不到 2cm，扫不动；
- 打印需要 300 dpi 的整数模块宽度，否则缩放采样会把模块边缘插值成灰边；
- 拍照要按距离分档，单码一页比一页塞八个好用。

产物（默认写进 `dataset/probe_qr_2026-11-16/print/`）：

- `二维码打印页_A4.pdf`：1 页说明 + 8 页单码 + 1 页总表，共 10 页；
- `preview_cover.png` / `preview_page.png`：不打印也能先看效果。

用法：
    python tools/make_qr_printsheet.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "dataset" / "probe_qr_2026-11-16"

# 直接复用生成器的 payload 表，避免两处各写一份码型
sys.path.insert(0, str(ROOT))
from tools.gen_qr_probe import PAYLOADS  # noqa: E402

A4 = (2480, 3508)          # 300 dpi
MARGIN = 200
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
BORDER_MODULES = 4


def font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def render_qr(payload: str, target_px: int) -> Image.Image:
    """按整数模块宽度栅格化，保证打印边缘干净。"""
    probe = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=1,
                          border=BORDER_MODULES)
    probe.add_data(payload)
    probe.make(fit=True)
    modules = probe.modules_count
    box = max(1, target_px // (modules + 2 * BORDER_MODULES))

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=box,
                       border=BORDER_MODULES)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L")


def new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    page = Image.new("L", A4, 255)
    return page, ImageDraw.Draw(page)


def cover_page() -> Image.Image:
    page, d = new_page()
    f_title, f_h, f_body, f_small = font(84), font(52), font(40), font(34)

    d.text((MARGIN, 220), "PhotoCheck 二维码实拍素材", fill=0, font=f_title)
    d.text((MARGIN, 340), "A4 · 300 dpi · 共 10 页（本页 + 8 页单码 + 1 页总表）",
           fill=90, font=f_small)

    y = 520
    d.text((MARGIN, y), "怎么用", fill=0, font=f_h); y += 90
    for line in (
        "1. 打印本文件：纸张 A4，缩放选「实际大小 / 100%」，黑白即可。",
        "2. 第 2–9 页，一页一个码。手机正对屏幕/纸张，距离 30 / 60 / 120 cm 各拍一张。",
        "3. 第 10 页是 8 码总表，用于远景或多码同框；也适合贴在墙上拍。",
        "4. 原图保存，不要裁剪、不要美颜、不要用微信压缩发送。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_body); y += 70

    y += 40
    d.text((MARGIN, y), "为什么必须拍，而不只是看合成图", fill=0, font=f_h); y += 90
    for line in (
        "现有 43 张 dev + 23 张 lock 全是程序生成的，没有拍摄角度、反光、摩尔纹。",
        "离线实测：模糊、斜拍都不影响解码，断崖在「拍屏的强摩尔纹」（0/8 解不出）。",
        "所以这批实拍的意义是补上「拍摄这一层」的证据，不是凑数量。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_body); y += 70

    y += 40
    d.text((MARGIN, y), "拍完怎么命名", fill=0, font=f_h); y += 90
    d.text((MARGIN, y), "qrshot-<码型>-<screen|paper>-<d30|d60|d120>-<序号>.jpg",
           fill=0, font=f_body); y += 80
    d.text((MARGIN, y), "例：qrshot-wifi-screen-d60-01.jpg", fill=90, font=f_small)

    y += 120
    d.text((MARGIN, y), "三条红线", fill=0, font=f_h); y += 90
    for line in (
        "· 只拍本文件里的码，不要换成真实收款码 / 名片 / 门禁码 / 群邀请。",
        "· 画面里不要出现身份证、工牌、快递单、聊天记录。",
        "· 不要人工打码——打码会破坏「是否漏检」的判据。",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_body); y += 70

    y += 110
    d.text((MARGIN, y), "拍完自查", fill=0, font=f_h); y += 90
    for line in (
        "□  24 张齐了（8 个码 × 3 个距离）",
        "□  全是原图，没裁剪、没美颜、没过微信压缩",
        "□  文件名按上面规则改过",
        "□  拍虚 / 拍歪的也留着（那是漏检样本，不能只交成功的）",
        "□  画面里没有真实证件、工牌、聊天记录",
    ):
        d.text((MARGIN, y), line, fill=0, font=f_body); y += 70

    d.text((MARGIN, A4[1] - 260), "PhotoCheck · C 侧探针素材 · 2026-11-16",
           fill=120, font=f_small)
    return page


def single_page(index: int, total: int, pid: str, subtype: str, payload: str) -> Image.Image:
    page, d = new_page()
    f_head, f_label, f_note, f_small = font(60), font(46), font(40), font(32)

    d.text((MARGIN, 200), f"#{index} / {total}   {pid} · {subtype}", fill=0, font=f_head)

    qr = render_qr(payload, 1480)
    x = (A4[0] - qr.width) // 2
    page.paste(qr, (x, 680))

    y = 680 + qr.height + 110
    d.text((MARGIN, y), "正对拍摄：30 cm ／ 60 cm ／ 120 cm 各一张", fill=0, font=f_label)
    d.text((MARGIN, y + 80), f"对应源文件 qr-{pid}-clean.png", fill=90, font=f_note)
    d.text((MARGIN, y + 156), "拍完别裁剪、别美颜；拍虚的那几张也留着。", fill=90, font=f_small)

    y += 300
    d.text((MARGIN, y), "拍完打勾", fill=0, font=f_note)
    y += 100
    box = 70
    for i, label in enumerate(("30 cm", "60 cm", "120 cm")):
        bx = MARGIN + i * 640
        d.rectangle([bx, y, bx + box, y + box], outline=0, width=4)
        d.text((bx + box + 26, y - 6), label, fill=0, font=f_label)

    d.text((MARGIN, A4[1] - 260), "PhotoCheck · C 侧探针素材 · 2026-11-16",
           fill=120, font=f_small)
    return page


def summary_page() -> Image.Image:
    page, d = new_page()
    f_head, f_label, f_small = font(64), font(44), font(36)
    d.text((MARGIN, 180), "8 码总表（远景 / 多码同框用）", fill=0, font=f_head)

    cols, rows = 2, 4
    cell_w = (A4[0] - 2 * MARGIN) // cols
    cell_h = 720
    top = 340

    qr_size = 460
    for i, (pid, subtype, payload) in enumerate(PAYLOADS):
        c, r = i % cols, i // cols
        x = MARGIN + c * cell_w
        y = top + r * cell_h
        qr = render_qr(payload, qr_size)
        page.paste(qr, (x + (cell_w - qr.width) // 2, y))
        d.text((x + 40, y + qr.height + 24), f"{i + 1}. {pid} · {subtype}",
               fill=0, font=f_label)
        d.text((x + 40, y + qr.height + 86), f"qr-{pid}-clean.png", fill=120, font=f_small)

    d.text((MARGIN, A4[1] - 260), "PhotoCheck · C 侧探针素材 · 2026-11-16",
           fill=120, font=f_small)
    return page


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 A4 打印页 PDF")
    ap.add_argument("--out", default=str(PROBE / "print"), help="输出目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pages = [cover_page()]
    for i, (pid, subtype, payload) in enumerate(PAYLOADS, start=1):
        pages.append(single_page(i, len(PAYLOADS), pid, subtype, payload))
    pages.append(summary_page())

    pdf = out / "二维码打印页_A4.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=300.0)

    pages[0].save(out / "preview_cover.png")
    pages[1].save(out / "preview_page.png")
    pages[-1].save(out / "preview_summary.png")

    size_mb = pdf.stat().st_size / 1024 / 1024
    print(f"[ok] {pdf}  共 {len(pages)} 页  {size_mb:.1f} MB")
    print("[ok] 预览：preview_cover.png / preview_page.png / preview_summary.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
