#!/usr/bin/env python3
"""掩码：**全项目唯一实现**（规格卡 §0.5 红线 1、K-08）。

为什么只能有一处：
  原始文字、二维码 payload 一律不得进任何可序列化记录。但只要允许"各处自己拼一下掩码"，
  就必然有人写成 `value[:3] + "****"`、有人写成 `value[0:3] + "****"`、有人忘了截断——
  轻则同一条号码在两处显示不一致，重则某个分支直接把原文漏进 masked_preview。
  掩码是**安全边界**，不是格式化工具，所以集中在这里，谁要显示都调它。

分工：
  - A 的检测器在产出 Detection 时调 `mask_for(kind, subtype, value)` 填 `masked_preview`
  - C 的生成器 / 标注转换调同一个函数
  - B 的处置与复检若需要在报告里引用，也调它

约束：
  - 输出的可见位数**固定**，不随输入长度变化泄露信息（手机号留 3+4，身份证留 4+4）
  - 非敏感类（meta / qr）不返回原文，只返回类别摘要——它们本来就没有"可以露一半"的字段
  - 不猜：长度不对的号码照样掩，不抛异常（异常会在批量处理里把整条流水线打断）
"""

from __future__ import annotations

# 手机号：前 3 后 4。11 位 → "138****0000"
PHONE_HEAD = 3
PHONE_TAIL = 4
PHONE_LEN = 11
# 身份证：前 4 后 4，中间固定 10 颗星。18 位 → "1101**********1234"
IDCARD_HEAD = 4
IDCARD_TAIL = 4
IDCARD_LEN = 18
IDCARD_STARS = 10

# 短输入保留 2 位可见前缀的**最低长度**。
# 8 位留 2 位 = 25% 可见，尚可；7 位留 2 位 = 29%，3 位留 2 位 = 67%——那不叫掩码。
# 阈值以下一律全遮，宁可认不出，也不漏。
PHONE_SHORT_KEEP_FROM = 8
IDCARD_SHORT_KEEP_FROM = 12

STAR = "*"


def _starred(head: str, tail: str, width: int) -> str:
    return f"{head}{STAR * width}{tail}"


def strip_country_code(value: str) -> str:
    """剥掉 `+86` / `0086` / `86`（13 位）前缀。

    2026-11-20 修正：A 的检测器在产出前会先剥区号，C 的掩码没有——
    同一个号码在两边形状不同：contract 输出 `+86****8000`（把区号露在外面），
    A 输出 `138****8000`。掩码是安全边界，两边必须逐字一致，所以统一在这里处理。
    """
    v = (value or "").strip()
    if v.startswith("+86"):
        return v[3:]
    if v.startswith("0086"):
        return v[4:]
    if v.startswith("86") and len(v) == 13:
        return v[2:]
    return v


def mask_phone(value: str) -> str:
    """手机号掩码。**只有恰好 11 位才用 3+4 的标准形状。**

    为什么按长度分档：`138****0000` 露出 7/11 ≈ 64%，这是 11 位号码下可接受的取舍
    （用户要认得出是哪条记录）。但对 8 位输入同样留 3+4 会露出 **7/8 = 88%**——
    那不是掩码，是只遮了一位。短输入多半来自 OCR 截断或错切，越短越该多遮。
    """
    value = strip_country_code(value)
    if not value:
        return ""          # 空输入返回空，别造出一个"*"让人以为遮到了什么
    if len(value) == PHONE_LEN:
        return _starred(value[:PHONE_HEAD], value[-PHONE_TAIL:], 4)
    if len(value) < PHONE_LEN:
        # 2026-11-16 修正：原实现对任意短输入都保留前 2 位，长度 1–3 时等于没遮
        # （"12" → "12*"、"123" → "12*"，67% 可见），与本节注释自相矛盾。
        if len(value) >= PHONE_SHORT_KEEP_FROM:
            return value[:2] + STAR * (len(value) - 2)
        return STAR * len(value)
    # 长于 11 位（带国家码、带分隔残留）：仍按 3+4，比例已低于标准形状
    return _starred(value[:PHONE_HEAD], value[-PHONE_TAIL:], 4)


def mask_idcard(value: str) -> str:
    """身份证号掩码。**只有恰好 18 位才用 4+10+4 的标准形状**，理由同上。

    12 位输入如果还留 4+4，会露出 8/12 = 67%；按短输入规则只留前 2 位。
    """
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) == IDCARD_LEN:
        return _starred(value[:IDCARD_HEAD], value[-IDCARD_TAIL:], IDCARD_STARS)
    if len(value) < IDCARD_LEN:
        # 同 mask_phone：短输入不再保留前 2 位，避免"只遮一位"的假掩码
        if len(value) >= IDCARD_SHORT_KEEP_FROM:
            return value[:2] + STAR * (len(value) - 2)
        return STAR * len(value)
    return _starred(value[:IDCARD_HEAD], value[-IDCARD_TAIL:], IDCARD_STARS)


def mask_summary(kind: str, subtype: str) -> str:
    """meta / qr 这类没有"半个值"可露的对象，只给类别摘要。"""
    return f"{kind}/{subtype}"


def mask_for(kind: str, subtype: str, value: str | None = None) -> str:
    """按类别选掩码方式。**调用方不要自己判断该调哪个**，统一走这里。"""
    if kind == "text" and subtype == "phone":
        return mask_phone(value or "")
    if kind == "text" and subtype == "idcard":
        return mask_idcard(value or "")
    return mask_summary(kind, subtype)
