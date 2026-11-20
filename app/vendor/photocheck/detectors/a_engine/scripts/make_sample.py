#!/usr/bin/env python
"""Generate **fictional** test samples: self-printed fake forms, fake numbers, self-made QR codes.

Spec card 0.5, privacy red line #5: demos always use fictional samples, never
someone else's real data. Every number below is computed, not taken from a person
(the ID check digit is derived with MOD 11-2, so it is "format-valid but belongs to
nobody").

The generator also records **ground truth at draw time** (`samples/gt/<image_id>.json`):
the boxes come from the drawing parameters (`ImageDraw.textbbox` / paste coordinates),
never from detector output - otherwise the evaluation is the detector proving itself.

The GT file holds **no plaintext**: only `kind` + `subtype` + `bbox`, never the digits,
so it can live in the repo. The numbers themselves stay as constants in this file and
are covered by unit tests when a value comparison is needed.

Purpose:
  - A self-tests K-03 / K-04 / K-05 / K-06 without waiting for C's dataset
  - input for `scripts/eval_detect.py` (R-02 baseline table)
  - backup material for the W8 demo recording

Usage:
    python scripts/make_sample.py                # write into samples/synthetic/ and samples/gt/
    python scripts/make_sample.py --out-dir /tmp/x
    python scripts/make_sample.py --no-gt        # images only, no ground truth
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

FAKE_ID = "110101199003070011"  # check digit from ISO 7064:1983 MOD 11-2, fictional
FAKE_PHONE = "13800138000"

# Boundary samples (K-13). Each one pins a rule that already regressed once or is
# one decision away from changing, so the sample is the cheapest possible guard.
FAKE_ID_X = "11010119900307002X"  # valid MOD 11-2, last char X (~1 in 11 real cards)
FAKE_ID_BAD_DATE = "110101199013320010"  # checksum passes, 13th month / 32nd day
FAKE_ID_BAD_CHECK = "110101199003070010"  # date fine, last digit flipped
FAKE_PHONE_INTL = "+86 138 0013 8000"  # written the way a contacts screenshot shows it
FAKE_PHONE_0086 = "0086-13900139000"


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _qr_array(payload: str, target_px: int = 220):
    import cv2

    encoder = cv2.QRCodeEncoder_create()
    code = encoder.encode(payload)
    if code is None:
        raise RuntimeError(f"QR encoding failed: {payload!r}")
    return cv2.resize(code, (target_px, target_px), interpolation=cv2.INTER_NEAREST)


def _paste_qr(image, payload: str, xy: tuple[int, int], size: int = 220):
    import numpy as np
    from PIL import Image

    code = _qr_array(payload, size)
    rgb = np.stack([code] * 3, axis=-1) if code.ndim == 2 else code
    image.paste(Image.fromarray(rgb), xy)


def _text_bbox(draw, xy: tuple[int, int], text: str, font) -> list[int]:
    """Text ground truth: computed by PIL, never eyeballed."""
    x0, y0, x1, y1 = draw.textbbox(xy, text, font=font)
    return [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]


def _tail_bbox(draw, xy: tuple[int, int], prefix: str, tail: str, font) -> list[int]:
    """Annotate only the tail of a line (e.g. `公民身份号码：<digits>` -> digits only)."""
    x, y = xy
    x_tail = x + int(round(draw.textlength(prefix, font=font)))
    return _text_bbox(draw, (x_tail, y), tail, font)


def _write_gt(
    gt_dir: Path,
    image_id: str,
    image_path: Path,
    objects: list[dict[str, Any]],
    note: str = "",
) -> Path:
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    # The note is report data (Chinese, consistent with the other deliverables).
    doc = {
        "image_id": image_id,
        "image": image_path.name,
        "width": width,
        "height": height,
        "source": "synthetic_generated",
        "generator": "scripts/make_sample.py",
        "objects": objects,
        "negative": not objects,
        "note": note or "真值由生成参数直接记录，非检测结果反推；不含号码明文。",
    }
    gt_dir.mkdir(parents=True, exist_ok=True)
    path = gt_dir / f"{image_id}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_express(out: Path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 620), "white")
    draw = ImageDraw.Draw(img)
    title, body, small = _font(34), _font(26), _font(20)
    objects: list[dict[str, Any]] = []

    # The drawn strings are the document content under test, so they stay Chinese.
    draw.rectangle([20, 20, 880, 600], outline="black", width=3)
    draw.text((44, 44), "校园快递代收单（虚构样本）", font=title, fill="black")
    draw.line([(40, 100), (860, 100)], fill="black", width=2)

    rows = [
        ("运单号", "SF0000000000001"),
        ("收件人", "李小雁"),
        ("联系电话", FAKE_PHONE),
        ("收件地址", "某某大学 3 号宿舍楼 412 室"),
        ("取件码", "8-2-1123"),
    ]
    y = 130
    for label, value in rows:
        draw.text((50, y), f"{label}：", font=body, fill="black")
        draw.text((230, y), value, font=body, fill="black")
        if label == "联系电话":
            objects.append(
                {
                    "gt_id": "g-phone-1",
                    "kind": "text",
                    "subtype": "phone",
                    "bbox": _text_bbox(draw, (230, y), value, body),
                }
            )
        y += 52

    id_prefix = "公民身份号码（代取登记）："
    draw.text((50, 430), id_prefix + FAKE_ID, font=small, fill="black")
    objects.append(
        {
            "gt_id": "g-idcard-1",
            "kind": "text",
            "subtype": "idcard",
            "bbox": _tail_bbox(draw, (50, 430), id_prefix, FAKE_ID, small),
        }
    )
    draw.text((50, 470), "备注：本单为演示虚构数据，号码不对应任何真实个人", font=small, fill="gray")
    _paste_qr(img, f"https://example.com/pickup/{FAKE_PHONE}", (640, 300))
    objects.append(
        {
            "gt_id": "g-qr-1",
            "kind": "qr",
            "subtype": "qr_link",
            # Paste coordinates plus size are the ground truth (quiet zone included),
            # not a box recovered by decoding.
            "bbox": [640, 300, 220, 220],
        }
    )

    path = out / "express_001.png"
    img.save(path)
    return path, objects


def make_idcard(out: Path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 480), (232, 240, 250))
    draw = ImageDraw.Draw(img)
    name_font, body = _font(36), _font(26)
    objects: list[dict[str, Any]] = []

    draw.rectangle([20, 20, 740, 460], outline=(120, 150, 190), width=3)
    draw.text((50, 46), "居民身份证（虚构样本）", font=_font(28), fill=(60, 80, 120))
    draw.text((50, 130), "姓名  张小雁", font=name_font, fill="black")
    id_prefix = "公民身份号码  "
    draw.text((50, 200), f"{id_prefix}{FAKE_ID}", font=body, fill="black")
    objects.append(
        {
            "gt_id": "g-idcard-1",
            "kind": "text",
            "subtype": "idcard",
            "bbox": _tail_bbox(draw, (50, 200), id_prefix, FAKE_ID, body),
        }
    )
    draw.text((50, 260), "住址  某某省某某市某某区某某路 1 号", font=body, fill="black")
    draw.text((50, 330), "签发机关  某某市公安局某某分局", font=body, fill="black")
    draw.text((50, 400), "有效期限  2020.01.01-2040.01.01", font=body, fill="black")

    path = out / "idcard_001.png"
    img.save(path)
    return path, objects


def make_wifi_qr(out: Path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (560, 640), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 30), "扫码连 Wi-Fi（虚构样本）", font=_font(30), fill="black")
    _paste_qr(img, "WIFI:S:Campus-Guest;T:WPA;P:demo1234;;", (120, 110), 320)
    draw.text((40, 470), "SSID：Campus-Guest", font=_font(24), fill="black")
    draw.text((40, 520), "密码：demo1234", font=_font(24), fill="black")

    objects = [
        {"gt_id": "g-qr-1", "kind": "qr", "subtype": "qr_wifi", "bbox": [120, 110, 320, 320]}
    ]

    path = out / "qr_wifi_001.png"
    img.save(path)
    return path, objects


def make_clean(out: Path):
    """Clean negative: text, no sensitive data, no codes. False alarms are caught here."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (800, 480), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 40), "社团活动通知（干净负样本）", font=_font(34), fill="black")
    lines = [
        "本周六下午三点在图书馆三楼报告厅举办分享会。",
        "主题：手机照片里的隐私边界。",
        "现场提供打印服务，可自带素材。",
        "报名人数上限 60 人，先到先得。",
    ]
    y = 130
    for line in lines:
        draw.text((40, y), line, font=_font(26), fill="black")
        y += 60

    path = out / "clean_001.png"
    img.save(path)
    return path, []


def make_multi_qr(out: Path):
    """Two codes on one page: the regression guard for the detectMulti unpacking bug.

    cv2 4.10's `detectMulti` returns two values, not four. The old code unpacked
    four, the ValueError was swallowed by the same `except` that means "no code
    found", so a page with two codes only ever reported one and nothing looked
    broken. A single-code sample cannot see that; this one can.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 460), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 24), "两张码同页（多码回归样本）", font=_font(30), fill="black")
    _paste_qr(img, "https://example.com/notice/42", (60, 120), 240)
    _paste_qr(img, "WIFI:S:Dorm-5F;T:WPA;P:demo1234;;", (560, 120), 240)
    draw.text((60, 386), "左：普通链接", font=_font(22), fill="black")
    draw.text((560, 386), "右：Wi-Fi 配置", font=_font(22), fill="black")

    objects = [
        {"gt_id": "g-qr-1", "kind": "qr", "subtype": "qr_link", "bbox": [60, 120, 240, 240]},
        {"gt_id": "g-qr-2", "kind": "qr", "subtype": "qr_wifi", "bbox": [560, 120, 240, 240]},
    ]
    path = out / "multi_qr_001.png"
    img.save(path)
    return path, objects


def make_half_qr(out: Path):
    """A code cut by the page edge - the sample that decides finder_min_count.

    The visible part keeps at most two finder patterns, and the evidence gate
    requires three, so this sample is *expected to be missed*. That is the point:
    it makes the D-1 trade-off measurable instead of theoretical. The GT box is
    the visible remainder, and it carries `known_gap` so the evaluator reports the
    miss separately instead of quietly scoring it as a regression.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (700, 440), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 24), "证件边角露出半个码（边界样本）", font=_font(28), fill="black")
    # Pasted at x=520 with a 300 px code on a 700 px page: 180 px visible, 60%.
    _paste_qr(img, "https://example.com/half", (520, 90), 300)

    objects = [
        {
            "gt_id": "g-qr-1",
            "kind": "qr",
            "subtype": "qr_unknown",
            "bbox": [520, 90, 180, 300],
            "known_gap": "BC-0011",
        }
    ]
    path = out / "qr_half_001.png"
    img.save(path)
    return path, objects


def make_phone_intl(out: Path):
    """Country-code phones, with the spacing people actually type.

    The redaction box has to start at the `+`, otherwise the country code stays
    visible and the "masked" number is still dialable.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 400), "white")
    draw = ImageDraw.Draw(img)
    body = _font(30)
    draw.text((40, 30), "名片式联系方式（区号回归样本）", font=_font(28), fill="black")
    objects: list[dict[str, Any]] = []

    for i, (label, tail, y) in enumerate(
        (("手机", FAKE_PHONE_INTL, 130), ("备用", FAKE_PHONE_0086, 210)), start=1
    ):
        prefix = f"{label}："
        draw.text((40, y), prefix + tail, font=body, fill="black")
        objects.append(
            {
                "gt_id": f"g-phone-{i}",
                "kind": "text",
                "subtype": "phone",
                # Annotate from the country code onward: that is what must be covered.
                "bbox": _tail_bbox(draw, (40, y), prefix, tail, body),
            }
        )

    draw.text((40, 300), "备注：虚构号码，不对应任何真实个人", font=_font(20), fill="gray")
    path = out / "phone_intl_001.png"
    img.save(path)
    return path, objects


def _idcard_page(out: Path, filename: str, title: str, number: str):
    """Shared layout for the three ID-number variants; returns (path, bbox)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 480), (232, 240, 250))
    draw = ImageDraw.Draw(img)
    body = _font(26)
    draw.rectangle([20, 20, 740, 460], outline=(120, 150, 190), width=3)
    draw.text((50, 46), title, font=_font(26), fill=(60, 80, 120))
    draw.text((50, 130), "姓名  张小雁", font=_font(34), fill="black")
    prefix = "公民身份号码  "
    draw.text((50, 200), f"{prefix}{number}", font=body, fill="black")
    bbox = _tail_bbox(draw, (50, 200), prefix, number, body)
    draw.text((50, 280), "住址  某某省某某市某某区某某路 1 号", font=body, fill="black")
    draw.text((50, 350), "签发机关  某某市公安局某某分局", font=body, fill="black")
    draw.text((50, 410), "虚构样本，号码由 MOD 11-2 计算得出，不对应任何真实个人", font=_font(18), fill="gray")

    path = out / filename
    img.save(path)
    return path, bbox


def make_idcard_x(out: Path):
    """Last char is X. About 1 in 11 real cards, and the first thing a digit-only
    regex gets wrong."""
    path, bbox = _idcard_page(out, "idcard_x_001.png", "居民身份证（末位 X 样本）", FAKE_ID_X)
    return path, [{"gt_id": "g-idcard-1", "kind": "text", "subtype": "idcard", "bbox": bbox}]


def make_idcard_bad_date(out: Path):
    """Checksum valid, calendar impossible -> deliberately dropped, so GT is empty.

    Labelling it as a negative is honest: per the agreed rule this string must not
    be reported. If someone later removes the birth-date check, this sample turns
    into a false alarm instead of a silent behaviour change.
    """
    path, _ = _idcard_page(
        out, "idcard_bad_date_001.png", "居民身份证（日期非法样本）", FAKE_ID_BAD_DATE
    )
    return path, []


def make_idcard_bad_check(out: Path):
    """Calendar fine, checksum wrong -> must still be reported, at risk 2.

    K-04 says a mistyped number leaks just as much as a correct one. Dropping it
    would be the one failure mode the user never notices.
    """
    path, bbox = _idcard_page(
        out, "idcard_bad_check_001.png", "居民身份证（校验位错样本）", FAKE_ID_BAD_CHECK
    )
    return path, [{"gt_id": "g-idcard-1", "kind": "text", "subtype": "idcard", "bbox": bbox}]


# GT notes are report data, so they stay Chinese like the other deliverables.
# Kept apart from the docstrings (which are code commentary and stay English).
SAMPLE_NOTES = {
    "make_express": "虚构快递单：手机号 + 身份证 + 链接码，K-03/K-04/K-05 的主用例",
    "make_idcard": "身份证页面，号码校验位合法、日期合法，应判 pass / risk 3",
    "make_wifi_qr": "Wi-Fi 配置码，SSID 走 K-06 的 safe_ssid 字段",
    "make_clean": "干净负样本：有文字、无敏感信息、无码，用于误报统计",
    "make_multi_qr": "同页两码：回归 detectMulti 解包缺陷，两个码都必须报出",
    "make_half_qr": "半张码：按 finder_min_count=3 预期漏检，缺口登记为 BC-0011",
    "make_phone_intl": "带 +86 / 0086 的手机号：遮盖框必须含区号，否则等于没盖",
    "make_idcard_x": "末位 X 的身份证：校验位按 MOD 11-2 算得 X，用于锁住字符集",
    "make_idcard_bad_date": "校验位通过对、日期非法（13 月 32 日）：按规则丢弃，故真值为空",
    "make_idcard_bad_check": "日期合法、校验位错：必须照报，risk 2，不得静默丢弃",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fictional test samples and ground truth")
    parser.add_argument("--out-dir", default=str(ROOT / "samples" / "synthetic"))
    parser.add_argument("--gt-dir", default=str(ROOT / "samples" / "gt"))
    parser.add_argument("--no-gt", action="store_true", help="generate images only, no ground truth")
    args = parser.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gt_dir = Path(args.gt_dir)

    makers = (
        make_express,
        make_idcard,
        make_wifi_qr,
        make_clean,
        make_multi_qr,
        make_half_qr,
        make_phone_intl,
        make_idcard_x,
        make_idcard_bad_date,
        make_idcard_bad_check,
    )
    for maker in makers:
        path, objects = maker(out)
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        if args.no_gt:
            print(f"generated {rel} ({len(objects)} gt object(s), not written)")
        else:
            gt_path = _write_gt(
                gt_dir, path.stem, path, objects, note=SAMPLE_NOTES.get(maker.__name__, "")
            )
            gt_rel = gt_path.relative_to(ROOT) if gt_path.is_relative_to(ROOT) else gt_path
            print(f"generated {rel}  +  {gt_rel} ({len(objects)} object(s))")

    print(f"\n{len(makers)} image(s) written to {out}")

    print("\nReminder: these are self-made samples, fine for self-testing and demos, but do NOT")
    print("register them as official dataset samples - K-13 samples need C's numbering,")
    print("annotation and generation record. The GT files hold no plaintext digits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
