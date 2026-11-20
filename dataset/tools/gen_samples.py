#!/usr/bin/env python3
"""PhotoCheck 合成样本生成器（C · K-13 / R-01）。

为什么用合成样本而不是实拍：
  1. 完全虚构，零版权与隐私风险（规格卡：以完全虚构样本为主）；
  2. **bbox 真值在绘制时直接得到**，不需要人工标框——人工标注只用来复核与处理后期的
     授权真实照片；
  3. 可复现：固定 seed，任何人跑一遍得到像素级一致的图与真值。

输出：
  dataset/dev/images/image-dev-XXX.jpg        合成图（含部分 EXIF）
  dataset/dev/labels/image-dev-XXX.json       真值：ImageDescriptor + Detection[]（契约 v4）
  dataset/dev/negatives.json                  「形似但不是」的负例框（契约里没有负例概念）
  dataset/dev/manifest.csv                    样本清单，供评测与报告引用

用法：
  python dataset/tools/gen_samples.py                # 生成 dev 全量（20 张）
  python dataset/tools/gen_samples.py --only 3,7     # 只重生成第 3、7 张
  python dataset/tools/gen_samples.py --out dataset/lock --seed-base 5000
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from contract.masking import mask_idcard, mask_phone  # noqa: E402
from contract.risk_rules import detection_risk  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_VERSION = "photocheck/v4"

FONT_DIR = Path("C:/Windows/Fonts")
FONT_BODY = FONT_DIR / "msyh.ttc"
FONT_TITLE = FONT_DIR / "msyhbd.ttc"
FONT_MONO = FONT_DIR / "simhei.ttf"

# 真实号段，用于生成合法手机号
PHONE_PREFIX = [
    "138", "139", "130", "131", "133", "135", "136", "150", "151", "152",
    "158", "159", "182", "183", "186", "187", "188", "189", "177", "199",
]
# 身份证前 6 位（虚构地区码，不对应真实行政区）
ID_AREA = ["110101", "310104", "440305", "510107", "330106", "420102", "370102"]
ID_WEIGHT = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID_CHECK = "10X98765432"

# 一张带 EXIF 的图会写进哪几类元数据——**这个元组是唯一来源**。
# `build_exif` 写了几个字段、`to_records` 就标几条、`manifest.meta_findings` 就填几。
# 原来三处各写各的：build_exif 写了 Software，真值只标 3 条，manifest 也写 3，
# 于是 B 的 K-01 检出 4 条、跟我们给的 3 条对不上，多出来那条被算成误报（2026-11-07）。
EXIF_SUBTYPES = ("gps", "datetime", "device", "software")


# --------------------------------------------------------------------------
# 造数
# --------------------------------------------------------------------------


def make_phone(rng: random.Random) -> str:
    return rng.choice(PHONE_PREFIX) + "".join(rng.choice("0123456789") for _ in range(8))


def id_checksum(body17: str) -> str:
    total = sum(int(ch) * w for ch, w in zip(body17, ID_WEIGHT))
    return ID_CHECK[total % 11]


def make_idcard(rng: random.Random, valid: bool = True, force_check: str | None = None) -> str:
    area = rng.choice(ID_AREA)
    birth = f"{rng.randint(1960, 2004)}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"
    seq = f"{rng.randint(0, 999):03d}"
    body = area + birth + seq
    check = id_checksum(body)
    if force_check is not None:
        # 只给「X 末位」样本用：换顺序码直到校验位命中目标。
        # 真实证件里 X 约占 1/11，不专门造一张就永远测不到这条分支。
        # force_check=None 时走下面的原路径，随机序列与历史完全一致（重生成 byte 幂等）。
        for _ in range(200):
            if check == force_check:
                return body + check
            seq = f"{rng.randint(0, 999):03d}"
            body = area + birth + seq
            check = id_checksum(body)
        raise RuntimeError(f"200 次没抽到校验位 {force_check}")  # pragma: no cover
    if not valid:
        # 校验位故意写错：格式完全像身份证，但校验不通过 —— 这是最容易误报的一类
        check = "1" if check != "1" else "2"
    return body + check


def make_intl_phone(rng: random.Random, fmt: str) -> str:
    """带国际区号的手机号，三种书写形态。

    为什么单独造：真实照片里 `+86 138...`、`0086-139...` 很常见（通讯录截图、名片），
    但 C 的 dev 集原来 31 个号码**全是裸 11 位**，A 支持了区号却没有任何样本能验。
    `fmt` 只影响前缀与分隔符，号码本体不变。
    """
    base = "1" + rng.choice("3456789") + "".join(rng.choice("0123456789") for _ in range(9))
    if fmt == "space":
        return f"+86 {base[:3]} {base[3:7]} {base[7:]}"
    if fmt == "dash":
        return f"0086-{base}"
    return f"+86{base}"


def strip_country_code(value: str) -> str:
    """剥掉 `+86` / `0086` 及其分隔符，只留 11 位。

    掩码必须**先剥再掩**：不剥就把区号露在外面、把号段藏起来，是反的。
    """
    v = value.strip().replace(" ", "").replace("-", "")
    if v.startswith("+86"):
        return v[3:]
    if v.startswith("0086"):
        return v[4:]
    return v


# 掩码不在这里实现：调 contract/masking.py（规格卡 §0.5 红线 1，全项目单点）
# 本地再写一份 = 迟早和 A 的检测器显示出两种掩码

# 负例文本：全都是「像手机号 / 像身份证但不是」的形态
NEGATIVES = {
    # 11 位纯数字，但首位是 2-9：最容易被手机号正则的宽松版本误判
    "order_no": lambda r: str(r.randint(2, 9)) + "".join(r.choice("0123456789") for _ in range(10)),
    "landline": lambda r: f"010-{r.randint(8200, 8899)}{r.randint(1000, 9999)}",
    "date_seq": lambda r: f"{r.randint(2024, 2026)}{r.randint(10, 12):02d}{r.randint(1, 28):02d}"
    + f"{r.randint(100, 999)}",
    "phone_like": lambda r: "1" + r.choice("0124567") + "".join(r.choice("0123456789") for _ in range(9)),
    # K-13 四条规范第 3 条点名要「少一位」
    "short_phone": lambda r: r.choice(PHONE_PREFIX) + "".join(r.choice("0123456789") for _ in range(7)),
    # 18 位、**校验位正确**、但第 7-14 位不是合法日期（月份 13、日 32）。
    # 校验位必须算对：若校验位也错，按 K-04 就得报 idcard_check=fail（risk 2），
    # 那它是一条 fail 正例而不是负例。2026-11-06 用 A 的检测器实测评 dev 集时，
    # 正是这条被报成 idcard(fail) 并计成误报，追下去才发现标注侧叠了两个错。
    "fake_id": lambda r: (lambda body: body + id_checksum(body))(
        "110101" + "20261332" + f"{r.randint(100, 999)}"
    ),
    # 19 位银联卡号：长度不对，也不该命中身份证规则
    "bank_card": lambda r: "62" + "".join(r.choice("0123456789") for _ in range(17)),
    "amount": lambda r: f"￥{r.randint(120, 9800)}.{r.randint(10, 99)}",
}


# --------------------------------------------------------------------------
# 绘制
# --------------------------------------------------------------------------


@dataclass
class Item:
    """一条待落进真值的对象。bbox 在画的时候顺手拿到。"""

    kind: str
    subtype: str
    bbox: list
    risk: int
    value: str = ""
    details: dict = field(default_factory=dict)


@lru_cache(maxsize=32)
def _load_font(path: str, size: int):
    """字体按 (路径, 字号) 缓存。

    一张图要调十几次 `font()`，生成 40 张就是几百次 `truetype`——每次都重新解析字体文件。
    字体对象是只读的，可以安全共享。实测生成 dev 全量 2997 ms → 2842 ms（约 5%），
    像素输出逐张比对完全一致；真正的瓶颈在二维码生成与 JPEG 编码，不在字体。
    """
    return ImageFont.truetype(path, size)


class Canvas:
    def __init__(self, size, bg=(250, 250, 247)):
        self.img = Image.new("RGB", size, bg)
        self.draw = ImageDraw.Draw(self.img)
        self.items: list[Item] = []
        self.negatives: list[dict] = []

    def font(self, size: int, bold: bool = False):
        path = FONT_TITLE if bold else FONT_BODY
        return _load_font(str(path), size)

    def text(self, xy, text, font, fill=(28, 28, 30)) -> tuple:
        bbox = self.draw.textbbox(xy, text, font=font)
        self.draw.text(xy, text, font=font, fill=fill)
        return bbox

    def tracked(self, xy, text, font, fill=(28, 28, 30), pad: int = 3) -> list:
        x0, y0, x1, y1 = self.text(xy, text, font, fill)
        return [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad]

    def add(self, kind, subtype, bbox, risk, value="", **details):
        self.items.append(Item(kind, subtype, bbox, risk, value, details))

    def add_negative(self, bbox, note):
        self.negatives.append({"bbox": bbox, "note": note})


def _phone_row(c: Canvas, y: int, label: str, value: str, size: int = 34):
    x = 96
    c.text((x, y), label, c.font(size), (95, 95, 100))
    c.add("text", "phone", c.tracked((x + 210, y), value, c.font(size), (24, 24, 26)), 2, value,
          masked_preview=mask_phone(value))


def build_receipt(rng: random.Random, spec: dict) -> Canvas:
    w, h = 1120, 1560
    c = Canvas((w, h))
    c.draw.rectangle([0, 0, w, 150], fill=(232, 68, 62))
    c.text((80, 46), rng.choice(["顺达快运", "中通速递", "圆通快递", "韵达快递"]), c.font(56, True), (255, 255, 255))
    c.text((80, 196), f"运单号 SF{rng.randint(10 ** 11, 10 ** 12 - 1)}", c.font(30), (90, 90, 95))

    y = 270
    c.draw.line([80, y - 16, w - 80, y - 16], fill=(200, 200, 200), width=2)

    name = rng.choice(["王建国", "李小雨", "陈志强", "赵静", "孙浩然"])
    c.text((96, y), "收件人", c.font(34), (95, 95, 100))
    c.text((306, y), f"{name}  {rng.choice(['先生', '女士'])}", c.font(36), (24, 24, 26))
    y += 82

    if spec.get("neg") == "order_no":
        value = NEGATIVES["order_no"](rng)
        c.text((96, y), "订单编号", c.font(34), (95, 95, 100))
        c.add_negative(c.tracked((306, y), value, c.font(34), (24, 24, 26)),
                       "11 位纯数字但首位是 2-9，不是手机号；宽松正则会把这类误报")
        y += 82

    if spec.get("phone"):
        value = make_phone(rng)
        _phone_row(c, y, "联系电话", value)
        y += 82

    if spec.get("neg") == "landline":
        box = c.tracked((306, y), NEGATIVES["landline"](rng), c.font(34))
        c.text((96, y), "座机", c.font(34), (95, 95, 100))
        c.add_negative(box, "座机号，11 位手机号规则不应命中")

    if spec.get("neg") in ("fake_id", "bank_card"):
        value = NEGATIVES[spec["neg"]](rng)
        c.text((96, y), "编号", c.font(34), (95, 95, 100))
        box = c.tracked((306, y), value, c.font(34), (24, 24, 26))
        note = ("18 位但生日段非法（13 月 32 日），是流水号不是证号"
                if spec["neg"] == "fake_id"
                else "19 位银联卡号，长度不符，不应命中身份证规则")
        c.add_negative(box, note)
        y += 82

    y += 82
    c.text((96, y), "收件地址", c.font(34), (95, 95, 100))
    addr = rng.choice([
        "北京市海淀区西土城路 10 号院 3 号楼 502",
        "上海市杨浦区四平路 1239 号 B 座 1104",
        "广州市天河区中山大道西 55 号 7 栋 203",
    ])
    c.text((306, y), addr[:16], c.font(32), (24, 24, 26))
    c.text((306, y + 44), addr[16:], c.font(32), (24, 24, 26))
    y += 150

    if spec.get("idcard"):
        valid = spec.get("idcard_valid", True)
        value = make_idcard(rng, valid=valid)
        box = c.tracked((306, y), value, c.font(34))
        c.text((96, y), "证件号", c.font(34), (95, 95, 100))
        # 校验位不通过仍然是「检出了身份证」——按规格卡 K-04 记 fail + risk 2，不是负例
        c.add("text", "idcard", box, 3 if valid else 2, value,
              masked_preview=mask_idcard(value), idcard_check="pass" if valid else "fail")
        y += 96

    # 内件品名：只为填满版面、增加干扰行，不含任何敏感信息
    y += 40
    c.text((96, y), "内件品名", c.font(30), (95, 95, 100))
    for i, goods in enumerate(["文件 / 合同", "衣物 × 1 件 · 0.8kg", "数码配件 × 1 件 · 0.3kg"]):
        c.text((306, y + i * 48), goods, c.font(28), (72, 72, 78))
    y += 48 * 3 + 20

    # 外框与分隔线（放固定位置会横向切过正文，改成随内容走的边框）
    c.draw.rectangle([64, 236, w - 64, y + 40], outline=(224, 224, 222), width=2)

    # 底部条码样式装饰（不标，纯干扰）
    x = 110
    while x < w - 140:
        bar = rng.randint(2, 7)
        c.draw.rectangle([x, h - 250, x + bar, h - 150], fill=(40, 40, 40))
        x += bar + rng.randint(3, 9)
    c.text((110, h - 120), "扫码查询物流 · 客服 400-800-1234", c.font(26), (110, 110, 115))
    return c


def build_chat(rng: random.Random, spec: dict) -> Canvas:
    w, h = 900, 1100
    c = Canvas((w, h), (237, 237, 237))
    c.draw.rectangle([0, 0, w, 110], fill=(247, 247, 247))
    c.text((360, 34), rng.choice(["小李", "张工", "房东王姐", "社团群"]), c.font(38, True), (30, 30, 30))

    y = 160
    # (who, text, tag)：tag 决定这句话落进真值的哪一类，靠字符串比较判断太脆
    lines: list[tuple[str, str, str]] = [
        ("them", rng.choice(["资料我发你，", "明天几点到？", "合同我看了没问题。"]), ""),
        ("me", "好的，我记一下", ""),
    ]
    phone = make_phone(rng) if spec.get("phone") else None
    if phone:
        lines.append(("them", "这是我新号", ""))
        lines.append(("them", phone, "phone"))
    if spec.get("idcard"):
        lines.append(("me", "身份证号发你核对", ""))
        lines.append(("me", make_idcard(rng), "idcard"))

    for who, text, tag in lines:
        f = c.font(32)
        tw = c.draw.textlength(text, font=f)
        pad = 28
        bw = int(tw) + 2 * pad
        if who == "them":
            x0 = 60
            c.draw.rounded_rectangle([x0, y, x0 + bw, y + 96], radius=18, fill=(255, 255, 255))
            box = c.tracked((x0 + pad, y + 24), text, f, (24, 24, 26))
        else:
            x0 = w - 60 - bw
            c.draw.rounded_rectangle([x0, y, x0 + bw, y + 96], radius=18, fill=(149, 236, 105))
            box = c.tracked((x0 + pad, y + 24), text, f, (20, 20, 20))

        if tag == "phone":
            c.add("text", "phone", box, 2, text, masked_preview=mask_phone(text))
        elif tag == "idcard":
            c.add("text", "idcard", box, 3, text, masked_preview=mask_idcard(text), idcard_check="pass")
        y += 130

    neg_kind = spec.get("neg")
    if neg_kind in ("phone_like", "short_phone"):
        value = NEGATIVES[neg_kind](rng)
        f = c.font(32)
        tw = c.draw.textlength(value, font=f)
        x0 = 60
        c.draw.rounded_rectangle([x0, y, x0 + int(tw) + 56, y + 96], radius=18, fill=(255, 255, 255))
        box = c.tracked((x0 + 28, y + 24), value, f, (24, 24, 26))
        note = ("11 位数字且首位为 1，但第二位不在 3-9 号段内，不是手机号"
                if neg_kind == "phone_like"
                else "手机号少一位（10 位），K-13 四条规范第 3 条点名要覆盖这类")
        c.add_negative(box, note)

    c.draw.rectangle([0, h - 110, w, h], fill=(247, 247, 247))
    c.text((40, h - 82), "发送消息…", c.font(30), (150, 150, 150))
    return c


def build_card(rng: random.Random, spec: dict) -> Canvas:
    w, h = 1080, 680
    c = Canvas((w, h), (248, 248, 250))
    c.draw.rectangle([0, 0, 16, h], fill=(38, 84, 168))
    c.text((90, 90), rng.choice(["张明远", "周子琪", "吴天成"]), c.font(66, True), (28, 28, 32))
    c.text((92, 186), rng.choice(["产品经理", "算法工程师", "项目经理"]), c.font(34), (100, 100, 108))
    c.text((92, 250), rng.choice(["星河科技有限公司", "云启智能", "北辰数据"]), c.font(32), (60, 60, 68))

    value = make_phone(rng) if spec.get("phone") else None
    if value:
        c.text((92, 330), value, c.font(38), (30, 30, 34))
        c.add("text", "phone", c.tracked((92, 330), value, c.font(38), (30, 30, 34)), 2, value,
              masked_preview=mask_phone(value))

    c.text((92, 400), "beijing@example-corp.cn", c.font(30), (90, 90, 100))
    c.text((92, 470), "北京市海淀区西土城路 10 号", c.font(28), (120, 120, 128))

    if spec.get("multi_qr"):
        # 两个码（名片 + Wi-Fi）。钉住 A 的 `detectMulti` 解包修复——
        # 旧实现 `ok, infos, points, _ = detectMulti(...)` 每次都抛 ValueError 被 except 吞掉，
        # 一张图有两个码也只会报第一个，而且外面看起来"正常返回"。
        paste_qr(c, rng, (600, 110), 240, "vcard", phone=value)
        paste_qr(c, rng, (600, 390), 200, "wifi")
    elif spec.get("qr"):
        paste_qr(c, rng, (720, 180), 300, spec.get("qr_kind", "vcard"),
                 phone=value, damaged=bool(spec.get("qr_damaged")))
    return c


def build_notice(rng: random.Random, spec: dict) -> Canvas:
    w, h = 1000, 1400
    c = Canvas((w, h), (252, 252, 250))
    c.text((240, 80), "入职登记表", c.font(54, True), (24, 24, 28))
    c.draw.line([120, 170, w - 120, 170], fill=(60, 60, 66), width=3)

    name = rng.choice(["刘洋", "许文博", "马晓东"])
    rows = [("姓　　名", name), ("部　　门", rng.choice(["研发中心", "市场部", "财务部"]))]
    y = 240
    for label, value in rows:
        c.text((130, y), label, c.font(34), (70, 70, 78))
        c.draw.line([330, y + 46, 860, y + 46], fill=(210, 210, 215), width=2)
        c.text((346, y), value, c.font(34), (26, 26, 30))
        y += 110

    if spec.get("phone"):
        value = make_phone(rng)
        c.text((130, y), "手机号码", c.font(34), (70, 70, 78))
        c.draw.line([330, y + 46, 860, y + 46], fill=(210, 210, 215), width=2)
        c.add("text", "phone", c.tracked((346, y), value, c.font(34), (26, 26, 30)), 2, value,
              masked_preview=mask_phone(value))
        y += 110

    if spec.get("idcard", True):
      force = "X" if spec.get("idcard_x") else None
      value = make_idcard(rng, valid=True, force_check=force)
      c.text((130, y), "身份证号", c.font(34), (70, 70, 78))
      c.draw.line([330, y + 46, 860, y + 46], fill=(210, 210, 215), width=2)
      c.add("text", "idcard", c.tracked((346, y), value, c.font(34), (26, 26, 30)), 3, value,
            masked_preview=mask_idcard(value), idcard_check="pass")
      y += 150

    if spec.get("intl_phone"):
        # 三种写法各一条正例：区号+空格 / 0086+短横 / 紧邻。
        # 掩码必须先剥区号再掩，否则区号露在外面、号段反而被藏起来。
        for label, fmt in (("国际号码", "space"), ("备用号码", "dash"), ("传真号码", "tight")):
            value = make_intl_phone(rng, fmt)
            c.text((130, y), label, c.font(32), (70, 70, 78))
            c.draw.line([330, y + 46, 860, y + 46], fill=(210, 210, 215), width=2)
            c.add("text", "phone", c.tracked((346, y), value, c.font(30), (26, 26, 30)), 2, value,
                  masked_preview=mask_phone(strip_country_code(value)))
            y += 88
        # 两条负例：带区号但位数不对（10 / 12 位），剥完前缀仍不该命中
        for bad in ("+86 " + "".join(rng.choice("0123456789") for _ in range(10)),
                    "0086-" + "".join(rng.choice("0123456789") for _ in range(12))):
            c.add_negative(c.tracked((346, y), bad, c.font(30), (26, 26, 30)),
                           "带区号但位数不对，剥前缀后应是 10 / 12 位，不应命中")
            y += 88

    if spec.get("neg") == "date_seq":
        box = c.tracked((346, y), NEGATIVES["date_seq"](rng), c.font(32))
        c.text((130, y), "工 号", c.font(34), (70, 70, 78))
        c.add_negative(box, "日期+流水号，13 位数字，易被切成 11 位手机号")

    c.text((130, h - 260), "本人承诺以上信息真实有效。", c.font(28), (80, 80, 88))
    c.text((700, h - 200), name, c.font(46), (40, 60, 130))
    c.draw.line([690, h - 140, 880, h - 140], fill=(60, 60, 66), width=2)
    return c


# --------------------------------------------------------------------------
# lock 专用模板：发票 / 工牌 / 录取通知书
# 这三类刻意与 dev 的 receipt/chat/card/notice 不同构，否则 lock 就等于从 dev 切出来的
# --------------------------------------------------------------------------


def build_invoice(rng: random.Random, spec: dict) -> Canvas:
    w, h = 1000, 1300
    c = Canvas((w, h), (252, 252, 255))
    c.draw.rectangle([0, 0, w, 120], fill=(30, 86, 160))
    c.text((70, 38), "增值税电子普通发票", c.font(42, True), (255, 255, 255))

    y = 160
    # clean：干净图要真的干净——连"看起来像手机号"的干扰项都不能印，
    # 否则它算误报还是算漏标，两条路都会污染指标
    if not spec.get("clean"):
        c.text((70, y), "发票号码", c.font(28), (100, 100, 108))
        no = NEGATIVES["order_no"](rng)
        c.add_negative(c.tracked((260, y), no, c.font(30), (28, 28, 32)),
                       "11 位纯数字但首位是 2-9，不是手机号")
    c.text((640, y), "开票日期 2026-11-0%d" % rng.randint(1, 9), c.font(26), (100, 100, 108))
    y += 80

    c.text((70, y), "购买方", c.font(28), (100, 100, 108))
    c.text((200, y), rng.choice(["北京星海科技有限公司", "南京云帆信息技术有限公司"]), c.font(30), (28, 28, 32))
    y += 62
    c.text((70, y), "销售方", c.font(28), (100, 100, 108))
    c.text((200, y), rng.choice(["上海岚图商贸有限公司", "深圳恒信电子有限公司"]), c.font(30), (28, 28, 32))
    y += 62

    if spec.get("neg") == "landline":
        v = NEGATIVES["landline"](rng)
        c.text((70, y), "联系电话", c.font(28), (100, 100, 108))
        c.add_negative(c.tracked((240, y), v, c.font(30), (28, 28, 32)),
                       "座机号带区号，11 位手机号规则不应命中")
        y += 62

    if spec.get("phone"):
        v = make_phone(rng)
        c.text((70, y), "联系电话", c.font(28), (100, 100, 108))
        c.add("text", "phone", c.tracked((240, y), v, c.font(30), (28, 28, 32)), 2, v,
              masked_preview=mask_phone(v))
        y += 62

    if spec.get("idcard"):
        valid = spec.get("idcard_valid", True)
        v = make_idcard(rng, valid=valid)
        c.text((70, y), "身份证号", c.font(28), (100, 100, 108))
        c.add("text", "idcard", c.tracked((240, y), v, c.font(30), (28, 28, 32)),
              3 if valid else 2, v, masked_preview=mask_idcard(v),
              idcard_check="pass" if valid else "fail")
        y += 62

    # 明细表
    y += 30
    c.draw.line([70, y, w - 70, y], fill=(180, 180, 200), width=2)
    y += 12
    for i, goods in enumerate(["*信息技术服务*技术服务费", "*办公用品*打印纸 A4", "价税合计（大写）壹仟贰佰元整"]):
        c.text((80, y + i * 52), goods, c.font(27), (40, 40, 48))
    y += 52 * 3 + 40
    c.text((700, y), "￥%d.00" % rng.randint(800, 5000), c.font(34, True), (30, 30, 36))

    if spec.get("qr"):
        paste_qr(c, rng, (690, h - 340), 260, spec.get("qr_kind", "link"))
        c.text((690, h - 70), "扫码核验", c.font(22), (120, 120, 128))
    return c


def build_badge(rng: random.Random, spec: dict) -> Canvas:
    w, h = 680, 1040
    c = Canvas((w, h), (250, 250, 252))
    c.draw.rectangle([0, 0, w, 110], fill=(16, 128, 108))
    c.text((40, 34), rng.choice(["星河科技", "云启智能", "北辰数据"]), c.font(36, True), (255, 255, 255))

    c.draw.rectangle([60, 160, 260, 420], fill=(226, 230, 236))
    c.text((104, 264), "照  片", c.font(30), (150, 156, 166))

    name = rng.choice(["陈嘉禾", "林可欣", "郑维"])
    c.text((300, 180), name, c.font(44, True), (26, 26, 30))
    c.text((300, 244), rng.choice(["研发中心 · 算法组", "产品部 · 用户研究"]), c.font(26), (90, 90, 98))

    y = 300
    if spec.get("neg") == "date_seq":
        c.text((300, y), "工号", c.font(24), (110, 110, 118))
        c.add_negative(c.tracked((360, y), NEGATIVES["date_seq"](rng), c.font(26), (30, 30, 34)),
                       "日期+流水号 13 位，易被切成 11 位手机号")
        y += 56
    if spec.get("neg") == "short_phone":
        c.text((300, y), "工号", c.font(24), (110, 110, 118))
        c.add_negative(c.tracked((360, y), NEGATIVES["short_phone"](rng), c.font(26), (30, 30, 34)),
                       "手机号少一位（10 位）")
        y += 56

    if spec.get("phone"):
        v = make_phone(rng)
        c.text((300, y), "手机", c.font(24), (110, 110, 118))
        c.add("text", "phone", c.tracked((360, y), v, c.font(26), (30, 30, 34)), 2, v,
              masked_preview=mask_phone(v))
        y += 56

    c.text((60, 470), "持卡人签名", c.font(22), (120, 120, 128))
    c.draw.line([60, 566, 420, 566], fill=(200, 200, 208), width=2)

    if spec.get("qr"):
        paste_qr(c, rng, (420, 640), 220, spec.get("qr_kind", "unknown"))
        c.text((60, 940), "门禁码 · 仅限本人使用", c.font(22), (120, 120, 128))
    return c


def build_admission(rng: random.Random, spec: dict) -> Canvas:
    w, h = 1000, 1400
    c = Canvas((w, h), (253, 252, 249))
    c.text((250, 90), "录 取 通 知 书", c.font(54, True), (150, 30, 30))
    c.draw.line([120, 180, w - 120, 180], fill=(150, 30, 30), width=3)

    c.text((120, 220), "%s 同学：" % rng.choice(["王思远", "刘一诺", "赵梓涵"]), c.font(30), (30, 30, 34))
    body = [
        "经审核，你已被我校%s专业录取，请于规定时间内办理入学手续。" % rng.choice(["计算机科学", "人工智能", "信息工程"]),
        "报到时须携带本通知书、身份证及学历证明材料。",
    ]
    for i, line in enumerate(body):
        c.text((120, 280 + i * 52), line, c.font(27), (48, 48, 54))

    y = 430
    if not spec.get("clean"):
        c.text((120, y), "考生号", c.font(26), (110, 110, 118))
        c.add_negative(c.tracked((250, y), NEGATIVES["order_no"](rng), c.font(28), (30, 30, 34)),
                       "11 位纯数字但首位是 2-9，不是手机号")
        y += 64

    if spec.get("neg") == "fake_id":
        c.text((120, y), "证件号", c.font(26), (110, 110, 118))
        c.add_negative(c.tracked((250, y), NEGATIVES["fake_id"](rng), c.font(28), (30, 30, 34)),
                       "18 位但生日段非法（13 月 32 日），是流水号不是证号")
        y += 64
    elif spec.get("neg") == "bank_card":
        c.text((120, y), "缴费卡号", c.font(26), (110, 110, 118))
        c.add_negative(c.tracked((250, y), NEGATIVES["bank_card"](rng), c.font(28), (30, 30, 34)),
                       "19 位银联卡号，长度不符，不应命中身份证规则")
        y += 64

    if spec.get("idcard"):
        v = make_idcard(rng, valid=True)
        c.text((120, y), "身份证号", c.font(26), (110, 110, 118))
        c.add("text", "idcard", c.tracked((250, y), v, c.font(28), (30, 30, 34)), 3, v,
              masked_preview=mask_idcard(v), idcard_check="pass")
        y += 64

    if spec.get("phone"):
        v = make_phone(rng)
        c.text((120, y), "联系电话", c.font(26), (110, 110, 118))
        c.add("text", "phone", c.tracked((250, y), v, c.font(28), (30, 30, 34)), 2, v,
              masked_preview=mask_phone(v))
        y += 64

    c.text((560, h - 200), "招生办公室", c.font(30), (60, 60, 68))
    c.text((560, h - 150), "2026 年 7 月", c.font(26), (90, 90, 98))

    if spec.get("qr"):
        paste_qr(c, rng, (110, h - 330), 230, spec.get("qr_kind", "link"))
    return c


BUILDERS = {
    "receipt": build_receipt,
    "chat": build_chat,
    "card": build_card,
    "notice": build_notice,
    "invoice": build_invoice,
    "badge": build_badge,
    "admission": build_admission,
}


# --------------------------------------------------------------------------
# 二维码：一处生成，四个模板共用
# --------------------------------------------------------------------------

QR_KINDS = ("vcard", "wifi", "pay", "link", "unknown")


def qr_payload(kind: str, rng: random.Random, phone: str | None = None) -> tuple[str, str]:
    """返回 (payload, subtype)。五类对应 K-06 的五个展示类。"""
    if kind == "wifi":
        return ("WIFI:T:WPA;S:Office-5G;P:%s;;"
                % "".join(rng.choice("0123456789") for _ in range(8)), "qr_wifi")
    if kind == "pay":
        return ("wxp://f2f0" + "".join(rng.choice("0123456789abcdef") for _ in range(20)),
                "qr_wechat_pay")
    if kind == "link":
        return (rng.choice(["https://example.com/notice/2026-11",
                            "https://example.org/form?id=%d" % rng.randint(10000, 99999)]),
                "qr_link")
    if kind == "unknown":
        return ("PX7" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24)),
                "qr_unknown")
    return ("BEGIN:VCARD\nVERSION:3.0\nFN:张明远\nTEL:%s\nEND:VCARD"
            % (phone or make_phone(rng)), "qr_vcard")


_QR_URL_HOST = re.compile(r"^https?://([^/?#]+)")
_QR_WIFI_SSID = re.compile(r"(?:^|;)S:([^;]*)")


def qr_safe_details(payload: str, subtype: str) -> dict:
    """把 payload 收敛成可落盘的安全摘要：只留域名 / SSID，绝不落原文。

    K-06 要求界面能显示"扫码会落到哪个网站 / 连哪个 Wi-Fi"，所以需要这两个字段；
    名片、收款码、不可解码类没有可展示的域名，只留 decodable。
    这是 DR-002 的裁定：qr 用 typed 字段，不再写通用 masked_summary。
    """
    if subtype == "qr_wifi":
        match = _QR_WIFI_SSID.search(payload)
        return {"safe_ssid": match.group(1)} if match and match.group(1) else {}
    if subtype == "qr_link":
        match = _QR_URL_HOST.match(payload)
        return {"safe_domain": match.group(1)} if match and match.group(1) else {}
    return {}


def paste_qr(c: "Canvas", rng: random.Random, pos: tuple, size: int,
             kind: str = "vcard", phone: str | None = None, damaged: bool = False) -> None:
    import qrcode

    payload, sub = qr_payload(kind, rng, phone)
    code = qrcode.QRCode(box_size=8, border=2)
    code.add_data(payload)
    code.make(fit=True)
    qr = code.make_image(fill_color=(20, 20, 20), back_color=(255, 255, 255)).convert("RGB")
    qr = qr.resize((size, size), Image.NEAREST)
    c.img.paste(qr, pos)
    c.draw.rectangle([pos[0], pos[1], pos[0] + size, pos[1] + size],
                     outline=(205, 205, 212), width=2)
    if damaged:
        band = size // 3
        c.draw.rectangle([pos[0], pos[1] + band, pos[0] + size, pos[1] + band * 2],
                         fill=(248, 248, 250))
    c.add("qr", sub, [pos[0], pos[1], size, size], 3, payload,
          decodable=not damaged, **qr_safe_details(payload, sub))


# --------------------------------------------------------------------------
# 规格表：20 张 dev 样本
# --------------------------------------------------------------------------

SPECS = [
    # 单据类 8 张：4 张带 EXIF GPS
    dict(no=1, tpl="receipt", seed=101, exif=True, phone=1, idcard=1),
    dict(no=2, tpl="receipt", seed=102, exif=True, phone=1),
    dict(no=3, tpl="receipt", seed=103, exif=False, phone=1, neg="order_no"),
    dict(no=4, tpl="receipt", seed=104, exif=True, phone=1, idcard=1, idcard_valid=False),
    dict(no=5, tpl="receipt", seed=105, exif=False, phone=1, neg="landline"),
    dict(no=6, tpl="receipt", seed=106, exif=True, phone=1),
    dict(no=7, tpl="receipt", seed=107, exif=False, phone=1, idcard=1),
    dict(no=8, tpl="receipt", seed=108, exif=False, phone=1),
    # 聊天截图 5 张
    dict(no=9, tpl="chat", seed=201, exif=False, phone=1, idcard=1),
    dict(no=10, tpl="chat", seed=202, exif=False, phone=1, neg="phone_like"),
    dict(no=11, tpl="chat", seed=203, exif=False, phone=1, idcard=1),
    dict(no=12, tpl="chat", seed=204, exif=False, phone=1),
    dict(no=13, tpl="chat", seed=205, exif=False, phone=1, neg="short_phone"),
    # 名片 + 二维码 4 张
    dict(no=14, tpl="card", seed=301, exif=True, phone=1, qr=1, qr_kind="vcard"),
    # 手机拍的名片本来就带 GPS：这张同时含 Wi-Fi 码与 EXIF，用来触发 K-10 的
    # 「Wi-Fi 二维码 + GPS = +3」组合加成（否则这条规则在全数据集里零命中）
    dict(no=15, tpl="card", seed=302, exif=True, phone=1, qr=1, qr_kind="wifi"),
    dict(no=16, tpl="card", seed=303, exif=False, phone=1, qr=1, qr_kind="wifi"),
    dict(no=17, tpl="card", seed=304, exif=True, phone=0, qr=1, qr_kind="vcard"),
    # 表单 3 张
    dict(no=18, tpl="notice", seed=401, exif=False, phone=1, neg="date_seq"),
    dict(no=19, tpl="notice", seed=402, exif=False, phone=1),
    dict(no=20, tpl="notice", seed=403, exif=True, phone=1),
    # 干净负样本：整张图不含任何敏感对象，专门测误报（K-13 矩阵要求 dev 6 张）
    dict(no=21, tpl="receipt", seed=501, exif=False),
    dict(no=22, tpl="chat", seed=502, exif=False),
    dict(no=23, tpl="notice", seed=503, exif=False),
    dict(no=24, tpl="card", seed=504, exif=False, qr=1, qr_kind="vcard"),
    # 坏图：健壮性测试，不计入检测指标（K-13 矩阵要求 5 张）
    dict(no=25, tpl="broken", broken="black"),
    dict(no=26, tpl="broken", broken="tiny"),
    dict(no=27, tpl="broken", broken="not_image"),
    # ---- 补齐 K-13 用例矩阵（27 -> 40）----
    # 二维码分类：补收款码 / 普通链接 / 未识别（含一张解不出的，测「解不出也要给框」）
    dict(no=28, tpl="card", seed=601, exif=False, phone=1, qr=1, qr_kind="pay"),
    dict(no=29, tpl="card", seed=602, exif=False, phone=1, qr=1, qr_kind="link"),
    dict(no=30, tpl="card", seed=603, exif=False, phone=1, qr=1, qr_kind="unknown", qr_damaged=True),
    # 身份证负样本：18 位非法日期流水号 / 19 位卡号
    dict(no=31, tpl="receipt", seed=604, exif=False, phone=1, neg="fake_id"),
    dict(no=32, tpl="receipt", seed=605, exif=False, phone=1, neg="bank_card"),
    # 干净负样本（无任何敏感对象），补到 6 张
    dict(no=33, tpl="receipt", seed=606, exif=False),
    dict(no=34, tpl="chat", seed=607, exif=False),
    dict(no=35, tpl="notice", seed=608, exif=False, idcard=0),
    dict(no=36, tpl="card", seed=609, exif=False),
    # 普通难度，凑满 40 张并把 hard 占比从 63% 拉回合理区间
    dict(no=37, tpl="receipt", seed=610, exif=False, phone=1),
    dict(no=38, tpl="notice", seed=611, exif=False, phone=1),
    # 坏图补到 5 张
    dict(no=39, tpl="broken", broken="wide"),
    dict(no=40, tpl="broken", broken="truncated"),
    # ── 2026-11-07 补三张：A 那边唯一还等 C 的实物 ──
    # 41 国际区号：+86 空格 / 0086 短横 / 紧邻，三正 + 两负（剥前缀后 10 / 12 位）
    dict(no=41, tpl="notice", seed=141, exif=False, phone=0, idcard=False, intl_phone=1),
    # 42 多码图：钉住 A 的 detectMulti 解包修复（一张图两个码必须都报）
    dict(no=42, tpl="card", seed=142, exif=False, phone=1, multi_qr=1),
    # 43 身份证 X 末位：真实证件里 X 约占 1/11，不专门造就永远测不到这条分支
    dict(no=43, tpl="notice", seed=143, exif=False, phone=1, idcard_x=1),
]


# lock 集：必须用 dev 没有的模板（发票 / 工牌 / 录取通知书），否则等于从 dev 里切出来，
# 同模板泄漏会让"锁定集"失去意义。K-13 四条规范第 1 条。
# 配额按 K-13 用例矩阵的 lock 列：二维码 5、手机号 3正/3负、身份证 3正/2负、干净 4。
LOCK_SPECS = [
    dict(no=1, tpl="invoice", seed=9001, exif=False, qr=1),
    dict(no=2, tpl="invoice", seed=9002, exif=False),
    dict(no=3, tpl="invoice", seed=9003, exif=False, qr=1, qr_kind="pay"),
    dict(no=4, tpl="invoice", seed=9004, exif=False, neg="landline"),
    dict(no=5, tpl="badge", seed=9005, exif=False, phone=1, qr=1, qr_kind="vcard"),
    dict(no=6, tpl="badge", seed=9006, exif=False, phone=1, neg="date_seq"),
    dict(no=7, tpl="badge", seed=9007, exif=False, phone=1, qr=1, qr_kind="unknown"),
    dict(no=8, tpl="admission", seed=9008, exif=False, idcard=1, phone=1),
    dict(no=9, tpl="admission", seed=9009, exif=False, idcard=1, qr=1),
    dict(no=10, tpl="admission", seed=9010, exif=False, idcard=1, neg="bank_card"),
    dict(no=11, tpl="admission", seed=9011, exif=False),
    dict(no=12, tpl="invoice", seed=9012, exif=False, neg="phone_like"),
    dict(no=13, tpl="badge", seed=9013, exif=False, neg="short_phone"),
    dict(no=14, tpl="admission", seed=9014, exif=False, neg="fake_id"),
    dict(no=15, tpl="invoice", seed=9015, exif=False, qr=1, qr_kind="pay"),
    dict(no=16, tpl="badge", seed=9016, exif=False),
    dict(no=17, tpl="admission", seed=9017, exif=False, phone=1),
    dict(no=18, tpl="invoice", seed=9018, exif=False, idcard=1),
    dict(no=19, tpl="badge", seed=9019, exif=False, qr=1, qr_kind="unknown"),
    dict(no=20, tpl="admission", seed=9020, exif=False),
    # 干净图补齐到 4 张（K-13 矩阵 lock 列要求 4 张，用来算误报率）。
    # clean=1 会连"发票号码 / 考生号"这类 11 位干扰项都不印——干净图里混一个
    # 陷阱，检测器报了它到底算误报还是算漏标，两条路都会污染指标。
    dict(no=21, tpl="invoice", seed=9021, exif=False, clean=1),
    dict(no=22, tpl="admission", seed=9022, exif=False, clean=1),
    dict(no=23, tpl="badge", seed=9023, exif=False, clean=1),
]

SPEC_SETS = {"dev": SPECS, "lock": LOCK_SPECS}

# 生成用的 seed 基。改这个值等于换一批图，lock 会因此失去封存意义
SEED_BASE = {"dev": 0, "lock": 5000}


# --------------------------------------------------------------------------
# 后期：噪声 / 模糊 / JPEG / EXIF
# --------------------------------------------------------------------------


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """模拟手机拍摄与转发压缩。不做几何旋转——旋转会让 bbox 真值漂移，
    那是「授权真实照片」那批样本要覆盖的事，不靠合成图勉强。"""
    if rng.random() < 0.5:
        img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 0.7)))
    if rng.random() < 0.7:
        noise = Image.effect_noise(img.size, rng.uniform(6, 14)).convert("L")
        img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), rng.uniform(0.02, 0.05))
    return img


def build_exif(rng: random.Random, size: tuple) -> dict:
    """构造一份「手机原图」的 EXIF：IFD0 放厂商/机型，Exif 子 IFD 放拍摄时间与像素尺寸，
    GPS 子 IFD 放坐标。

    用 piexif 而不是 Pillow 直接写：Pillow 的 `Exif.get_ifd()` 在 10.4 上改了子 IFD
    不会序列化出去（13.x 才可以），结果是**图片看起来有 EXIF、GPS 和时间却静默丢失**。
    piexif 是显式 IFD 结构，版本无关，而且它本来就是规格卡 §0.3 指定的 EXIF 工具。
    """
    import piexif

    def dms(deg: int, minute: int, sec_100: int):
        return ((deg, 1), (minute, 1), (sec_100, 100))

    stamp = f"2026:10:{rng.randint(10, 28):02d} {rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"

    return {
        "0th": {
            piexif.ImageIFD.Make: rng.choice(["Xiaomi", "HUAWEI", "OPPO", "vivo"]).encode(),
            piexif.ImageIFD.Model: rng.choice(["MI 13", "P60", "Find X6", "X90"]).encode(),
            piexif.ImageIFD.Software: b"CameraApp 5.2",
            piexif.ImageIFD.DateTime: stamp.encode(),
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp.encode(),
            piexif.ExifIFD.DateTimeDigitized: stamp.encode(),
            piexif.ExifIFD.PixelXDimension: size[0],
            piexif.ExifIFD.PixelYDimension: size[1],
        },
        "GPS": {
            piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: dms(rng.randint(39, 40), rng.randint(0, 59), rng.randint(0, 5999)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: dms(rng.randint(116, 121), rng.randint(0, 59), rng.randint(0, 5999)),
        },
        "1st": {},
        "thumbnail": None,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def to_records(image_id: str, size, items: list[Item], has_exif: bool) -> list[dict]:
    records = [
        {
            "contract_version": CONTRACT_VERSION,
            "record_type": "ImageDescriptor",
            "image_id": image_id,
            "width": size[0],
            "height": size[1],
            "orientation": "exif_transposed",
            "coordinate_space": "top_left_pixels",
        }
    ]
    if has_exif:
        # 四个 subtype 必须与 build_exif 实际写进去的字段一一对应。
        # 原来漏了 software——生成器写了 `ImageIFD.Software`，真值却没标，
        # B 的 K-01 老老实实检出 4 条、我们只给了 3 条，多出来那条被算成误报。
        # risk 也不再硬编码：统一问 risk_rules，避免"表里是 2、这里写 1"这种漂移。
        for subtype in EXIF_SUBTYPES:
            records.append(_meta(image_id, subtype, detection_risk("meta", subtype, {})))
    for idx, item in enumerate(items, start=1):
        records.append(
            {
                "contract_version": CONTRACT_VERSION,
                "record_type": "Detection",
                "id": f"det-{image_id}-{item.kind}-{idx:02d}",
                "image_id": image_id,
                "kind": item.kind,
                "subtype": item.subtype,
                "bbox": item.bbox,
                "detector_score": None,
                "evidence": {
                    "meta": "exif",
                    "text": "ocr_rule",
                    "qr": "qr_payload",
                    "face": "face_box",
                }[item.kind],
                # 分值不硬编码：与 A 的检测器共用 contract/risk_rules.py
                "risk": detection_risk(item.kind, item.subtype, item.details),
                "details": dict(item.details),
            }
        )
    return records


def _meta(image_id: str, subtype: str, risk: int = 0) -> dict:  # risk 参数保留但不再使用
    return {
        "contract_version": CONTRACT_VERSION,
        "record_type": "Detection",
        "id": f"det-{image_id}-meta-{subtype}",
        "image_id": image_id,
        "kind": "meta",
        "subtype": subtype,
        "bbox": None,
        "detector_score": None,
        "evidence": "exif",
        "risk": detection_risk("meta", subtype, {}),
        "details": {},
    }


def scene_of(spec: dict) -> str:
    """scene_id 绑定模板：同模板只能整体进 dev 或整体进 lock，否则锁定集失效。"""
    return f"scene-{spec['tpl']}"


def difficulty_of(spec: dict) -> str:
    """难度先按模板 + 是否含负例粗分，W4 前再人工复核一遍。"""
    return "hard" if spec["tpl"] in ("chat", "card") or spec.get("neg") else "easy"


def _generate_broken(spec: dict, out_root: Path) -> dict:
    """坏图：只生成文件，不生成标注（它们不进检测指标，只测 K-01 健壮性）。"""
    image_id = f"image-{out_root.name}-{spec['no']:03d}"
    img_dir = out_root / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / f"{image_id}.jpg"

    kind = spec["broken"]
    if kind == "black":
        Image.new("RGB", (640, 480), (0, 0, 0)).save(path, "JPEG", quality=70)
        size = (640, 480)
    elif kind == "tiny":
        Image.new("RGB", (16, 16), (200, 200, 200)).save(path, "JPEG", quality=70)
        size = (16, 16)
    elif kind == "wide":
        im = Image.new("RGB", (4000, 200), (238, 238, 236))
        d = ImageDraw.Draw(im)
        f = ImageFont.truetype(str(FONT_BODY), 28)
        for i in range(24):
            d.text((30 + i * 165, 84), "宽幅长图 %02d" % i, font=f, fill=(70, 70, 78))
        im.save(path, "JPEG", quality=72)
        size = (4000, 200)
    elif kind == "truncated":
        Image.new("RGB", (800, 600), (160, 160, 158)).save(path, "JPEG", quality=85)
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) * 6 // 10])  # 截掉后 40%，解码应当失败
        size = (800, 600)
    else:  # not_image：扩展名是 jpg，内容不是图片
        path.write_bytes("这不是一张图片，用来测载入是否会崩。".encode("utf-8"))
        size = (0, 0)

    return {
        "image_id": image_id,
        "file": f"images/{image_id}.jpg",
        "set": out_root.name,
        "scene_id": "scene-broken",
        "template": "broken",
        "difficulty": "hard",
        "width": size[0],
        "height": size[1],
        "quality": 70,
        "has_exif": 0,
        "meta_findings": 0,
        "positives": 0,
        "negatives": 0,
    }


def generate(spec: dict, out_root: Path, seed_base: int) -> dict:
    image_id = f"image-{out_root.name}-{spec['no']:03d}"
    if spec["tpl"] == "broken":
        return _generate_broken(spec, out_root)
    rng = random.Random(seed_base + spec["seed"])
    canvas = BUILDERS[spec["tpl"]](rng, spec)
    img = degrade(canvas.img, rng)

    img_dir = out_root / "images"
    lbl_dir = out_root / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    quality = rng.randint(62, 88)
    exif = build_exif(rng, img.size) if spec.get("exif") else None
    path = img_dir / f"{image_id}.jpg"
    img.save(path, "JPEG", quality=quality)
    if exif is not None:
        import piexif

        piexif.insert(piexif.dump(exif), str(path))

    records = to_records(image_id, img.size, canvas.items, bool(spec.get("exif")))
    (lbl_dir / f"{image_id}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )

    return {
        "image_id": image_id,
        "file": f"images/{image_id}.jpg",
        "template": spec["tpl"],
        "scene_id": scene_of(spec),
        "difficulty": difficulty_of(spec),
        "width": img.size[0],
        "height": img.size[1],
        "quality": quality,
        "has_exif": int(bool(spec.get("exif"))),
        "meta_findings": len(EXIF_SUBTYPES) if spec.get("exif") else 0,
        "positives": len(canvas.items),
        "negatives": len(canvas.negatives),
        "set": out_root.name,
        "_negatives": canvas.negatives,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 PhotoCheck 合成样本")
    ap.add_argument("--set", choices=sorted(SPEC_SETS), default="dev",
                    help="生成哪一套：dev（默认）或 lock")
    ap.add_argument("--out", default=None, help="输出目录，默认 dataset/<set>")
    # 每个集合有固定的 seed 基：不固定的话，同一份数据在两次生成之间会变成另一批图，
    # 封存哈希随之失效。lock 当初是用 5000 生成的，写死在这里，`--set lock` 即可复现。
    ap.add_argument("--seed-base", type=int, default=None,
                    help="默认 dev=0 / lock=5000；除非要造新集，不要改")
    ap.add_argument("--only", default="", help="只生成指定编号，逗号分隔，如 3,7")
    args = ap.parse_args(argv)

    out_root = Path(args.out) if args.out else ROOT / "dataset" / args.set
    seed_base = args.seed_base if args.seed_base is not None else SEED_BASE[args.set]
    only = {int(x) for x in args.only.split(",") if x.strip()}
    specs = [s for s in SPEC_SETS[args.set] if not only or s["no"] in only]

    rows, negatives = [], {}
    for spec in specs:
        info = generate(spec, out_root, seed_base)
        negatives[info["image_id"]] = info.pop("_negatives", [])
        rows.append(info)

    neg_path = out_root / "negatives.json"
    existing = {}
    if neg_path.exists():
        existing = json.loads(neg_path.read_text(encoding="utf-8"))
    existing.update(negatives)
    neg_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")

    manifest = out_root / "manifest.csv"
    fields = ["image_id", "file", "set", "scene_id", "template", "difficulty",
              "width", "height", "quality", "has_exif", "meta_findings",
              "positives", "negatives"]
    with open(manifest, "w", newline="", encoding="utf-8-sig") as fh:
        # lineterminator="\n"：默认的 \r\n 会被 git 规范化，每次重生成都刷 warning
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    total_pos = sum(r["positives"] for r in rows)
    total_neg = sum(r["negatives"] for r in rows)
    print(f"生成 {len(rows)} 张 → {out_root}")
    print(f"  正向对象 {total_pos} 条，负例 {total_neg} 条，带 EXIF {sum(r['has_exif'] for r in rows)} 张")
    print(f"  清单 {manifest.name} / 负例 negatives.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
