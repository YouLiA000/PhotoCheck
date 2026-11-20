#!/usr/bin/env python3
"""二维码探针图生成器：干净源图 + 模拟手机拍摄的退化图。

用途：现有 dev / lock 全是合成图（无拍摄角度、文字偏清晰、不含真实拍摄噪声），
缺「真实拍照二维码」这一层证据。本工具生成两类素材：

1. `source/`  —— 干净二维码，用于**打印或投屏后用手机实拍**；
2. `degraded/` —— 在源图上施加拍摄相关退化（远距小尺寸、失焦、透视、屏幕摩尔纹、
   JPEG 压缩），用于**离线**跑鲁棒性，不必等实拍。

三条硬约束：

- **隐私**：payload 全部是构造的假数据，不含任何真实收款码 / 名片 / 手机号；
  且 payload 本身**不写入 manifest**（只在内存里），符合「二维码 payload 不进 CSV」红线。
- **可解性门禁**：每张源图必须被 pyzbar 解出且内容与写入一致，否则不写盘；
  退化图解不出是允许的（那正是漏检样本），但要如实记进 manifest。
- **不污染数据集**：产物落在 `dataset/probe_qr_2026-11-16/`，**不属于 dev、不属于 lock**，
  不得用于调参，也不进封存哈希。

用法：
    python tools/gen_qr_probe.py            # 生成到默认目录
    python tools/gen_qr_probe.py --out DIR  # 指定输出目录
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import shutil
import sys
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from qrcode.constants import ERROR_CORRECT_H

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dataset" / "probe_qr_2026-11-16"

# 每项：(id, 期望 subtype, payload)。payload 只存在于内存，不落 CSV。
# subtype 命名对齐 contract/risk_rules.py 的枚举，便于和真值口径对照。
PAYLOADS: tuple[tuple[str, str, str], ...] = (
    ("wifi", "qr_wifi", "WIFI:T:WPA;S:PhotoCheck_Probe;P:not-a-real-key;;"),
    ("link", "qr_link", "https://example.com/photocheck/probe?case=R1"),
    ("geo", "qr_geo", "geo:39.9042,116.4074"),
    ("vcard", "qr_vcard", "BEGIN:VCARD\nVERSION:3.0\nFN:Probe Contact\nTEL:00000000000\nEND:VCARD"),
    ("pay", "qr_wechat_pay", "wxp://f2f0" + "0" * 18),
    ("alipay", "qr_alipay", "alipays://platformapi/startapp?appId=00000000"),
    ("tel", "qr_contact", "tel:00000000000"),
    ("text", "qr_unknown", "PHOTOCHECK-PROBE-2026-11-16"),
)

# 退化条件：(条件名, 说明)。clean 由 source/ 承担，不重复生成。
# 顺序即难度梯度，便于看「从哪一档开始解不出」。
CONDITIONS: tuple[tuple[str, str], ...] = (
    ("small", "整码缩到 96px 再贴到大画布，模拟远距离拍摄"),
    ("blur_soft", "高斯模糊 radius=2.0，模拟轻微失焦"),
    ("blur_hard", "高斯模糊 radius=4.0，模拟明显失焦"),
    ("perspective", "旋转 12° + 四边形透视，模拟斜拍"),
    ("moire_soft", "细网格 + 轻微模糊，模拟拍屏但像素格较疏"),
    ("moire", "密网格 + 模糊，模拟近距离拍屏的强摩尔纹"),
    ("jpeg", "真实 JPEG 往返一次（quality=35），模拟压缩损失"),
)

CANVAS = (900, 620)  # 与规格卡参考集 R1/R2 同尺寸，便于与 K-03 对照
SEED_MODULE_PX = 10
BORDER_MODULES = 4


def validate_force_target(out: Path) -> Path:
    """限制 `--force` 的删除范围。

    输出目录必须位于仓库 `dataset/` 下，且名称以 `probe_qr_` 开头。
    防止 `--out . --force`、`--out D:\\ --force` 之类误删仓库或整块磁盘。
    """
    resolved = out.resolve()
    dataset_root = (ROOT / "dataset").resolve()
    try:
        resolved.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError(f"--force 只允许清理 {dataset_root} 下的探针目录：{resolved}") from exc
    if resolved == dataset_root or not resolved.name.startswith("probe_qr_"):
        raise ValueError(f"--force 目标必须是 dataset/probe_qr_*：{resolved}")
    return resolved


def build_qr(payload: str) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=SEED_MODULE_PX,
        border=BORDER_MODULES,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L")


def paste_center(canvas: Image.Image, code: Image.Image) -> Image.Image:
    out = canvas.copy()
    x = (out.width - code.width) // 2
    y = (out.height - code.height) // 2
    out.paste(code, (x, y))
    return out


def degrade(code: Image.Image, condition: str) -> Image.Image:
    canvas = Image.new("L", CANVAS, 255)

    if condition == "small":
        small = code.resize((96, 96), Image.LANCZOS)
        return paste_center(canvas, small)

    base = paste_center(canvas, code)

    if condition == "blur_soft":
        return base.filter(ImageFilter.GaussianBlur(2.0))

    if condition == "blur_hard":
        return base.filter(ImageFilter.GaussianBlur(4.0))

    if condition == "perspective":
        rotated = base.rotate(12, resample=Image.BICUBIC, expand=False, fillcolor=255)
        w, h = rotated.size
        inset_x, inset_y = int(w * 0.06), int(h * 0.04)
        quad = (inset_x, inset_y, w - inset_x, inset_y + 6,
                w - inset_x - 8, h - inset_y, inset_x + 10, h - inset_y)
        return rotated.transform(rotated.size, Image.QUAD, quad, resample=Image.BICUBIC)

    if condition == "moire_soft":
        # 正弦亮度调制 + 轻微模糊：模拟拍屏，但像素格还没细到切断模块。
        # 用条纹而非硬线段，是因为硬网格会直接把模块切开，那不是「轻」而是另一种破坏。
        px = base.load()
        for x in range(base.width):
            factor = 1.0 - 0.06 * (1 + math.sin(2 * math.pi * x / 9))
            for y in range(base.height):
                px[x, y] = int(px[x, y] * factor)
        return base.filter(ImageFilter.GaussianBlur(0.6))

    if condition == "moire":
        striped = base.copy()
        draw = ImageDraw.Draw(striped)
        for x in range(0, striped.width, 3):
            draw.line([(x, 0), (x, striped.height)], fill=232, width=1)
        for y in range(0, striped.height, 3):
            draw.line([(0, y), (striped.width, y)], fill=240, width=1)
        return striped.filter(ImageFilter.GaussianBlur(1.0))

    if condition == "jpeg":
        buf = io.BytesIO()
        base.save(buf, format="JPEG", quality=35)
        buf.seek(0)
        return Image.open(buf).convert("L")

    raise ValueError(f"未知条件：{condition}")


def decode(path: Path) -> str | None:
    """返回解出的 payload；解不出返回 None。"""
    try:
        from pyzbar.pyzbar import decode as zb_decode
    except Exception:  # noqa: BLE001 - 环境缺 pyzbar 时退化为「未验证」
        return None
    data = zb_decode(Image.open(path))
    return data[0].data.decode("utf-8", "replace") if data else None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contact_sheet(cards: list[tuple[str, str, str, Path]], out: Path) -> None:
    """生成一页可投屏/打印的对照表，方便按顺序实拍。"""
    width = 900
    row_h = 470
    height = 140 + row_h * len(cards)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
        small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
    except Exception:  # noqa: BLE001
        font = small = ImageFont.load_default()

    draw.text((40, 40), "PhotoCheck 二维码实拍素材（拍摄顺序自上而下）", fill="black", font=font)

    y = 110
    for title, subtitle, _subtype, path in cards:
        img = Image.open(path).convert("L")
        img = img.resize((200, 200), Image.LANCZOS)
        sheet.paste(img, (40, y))
        draw.text((270, y + 30), title, fill="black", font=font)
        draw.text((270, y + 80), subtitle, fill="black", font=small)
        draw.text((270, y + 120), f"文件：{path.name}", fill="black", font=small)
        y += row_h

    sheet.save(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成二维码探针图（源图 + 退化图）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--force", action="store_true", help="目录已存在时清空重建")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        if not args.force:
            print(f"[error] 目录已存在：{out}；加 --force 重建", file=sys.stderr)
            return 2
        try:
            out = validate_force_target(out)
        except ValueError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        shutil.rmtree(out)
    (out / "source").mkdir(parents=True)
    (out / "degraded").mkdir()

    rows: list[dict[str, str]] = []
    cards: list[tuple[str, str, str, Path]] = []

    for pid, subtype, payload in PAYLOADS:
        code = build_qr(payload)
        src_path = out / "source" / f"qr-{pid}-clean.png"
        code.save(src_path)

        got = decode(src_path)
        if got != payload:
            print(f"[fatal] 源图不可解：{src_path.name}（解出 {got!r}）", file=sys.stderr)
            return 1

        rows.append({
            "file": f"source/{src_path.name}", "group": "source", "payload_id": pid,
            "expected_subtype": subtype, "condition": "clean",
            "width": str(code.width), "height": str(code.height),
            "decodable": "yes", "sha256": sha256_of(src_path),
            "note": "用于打印或投屏后实拍",
        })
        cards.append((f"{pid} · {subtype}", "干净源图：请投屏或打印后拍摄", subtype, src_path))

        for cond, note in CONDITIONS:
            img = degrade(code, cond)
            dst = out / "degraded" / f"qr-{pid}-{cond}.png"
            img.save(dst)
            rows.append({
                "file": f"degraded/{dst.name}", "group": "degraded", "payload_id": pid,
                "expected_subtype": subtype, "condition": cond,
                "width": str(img.width), "height": str(img.height),
                "decodable": "yes" if decode(dst) == payload else "no",
                "sha256": sha256_of(dst), "note": note,
            })

    manifest = out / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    contact_sheet(cards, out / "contact_sheet.png")

    ok = sum(1 for r in rows if r["decodable"] == "yes")
    print(f"[ok] {len(PAYLOADS)} 个源码 / {len(rows)} 张图 -> {out}")
    print(f"[ok] 可解 {ok} 张，不可解 {len(rows) - ok} 张（退化图解不出属预期）")
    print(f"[ok] manifest: {manifest.name}（不含 payload）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
