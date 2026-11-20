"""掩码单点实现的测试（§0.5 红线 1）。

掩码是**安全边界**，不是格式化工具：写错一位，原文就跟着报告外流。
所以这里不只测"格式化对不对"，更测**任何输入都不会整串返回**。

    python -m unittest contract.tests.test_masking -v
"""

from __future__ import annotations

import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract import masking  # noqa: E402

PHONE_SHAPE = re.compile(r"^\d{3}\*{4}\d{4}$")
IDCARD_SHAPE = re.compile(r"^[0-9]{4}\*{10}[0-9Xx]{4}$")


class Phone(unittest.TestCase):
    def test_standard_shape(self):
        self.assertEqual(masking.mask_phone("13931298426"), "139****8426")

    def test_keeps_length(self):
        for value in ("13800138000", "19912345678"):
            with self.subTest(value=value):
                self.assertEqual(len(masking.mask_phone(value)), len(value))

    def test_never_returns_whole_value(self):
        # 这条是红线的核心：无论输入多短，都不能原样吐回去
        for value in ("", "1", "12", "123", "1234", "1234567", "12345678"):
            with self.subTest(value=value):
                out = masking.mask_phone(value)
                if value:
                    self.assertNotEqual(out, value, "短号码被原样返回")
                    self.assertIn("*", out)

    def test_tiny_fragments_are_fully_masked(self):
        # 1–7 位一个字符都不许露：旧实现返回 "12*"，等于没遮
        for value in ("1", "12", "123", "1234567"):
            with self.subTest(value=value):
                out = masking.mask_phone(value)
                self.assertEqual(out, "*" * len(value), out)
                self.assertFalse(any(ch.isdigit() for ch in out), out)

    def test_plausible_truncation_keeps_two(self):
        # 8–10 位是真实会遇到的 OCR 截断：留 2 位既能认出记录，又不超 25% 可见
        for value in ("12345678", "123456789", "1234567890"):
            with self.subTest(value=value):
                out = masking.mask_phone(value)
                self.assertTrue(out.startswith(value[:2]), out)
                self.assertEqual(len(out), len(value))
                self.assertEqual(out.count("*"), len(value) - 2)

    def test_strips_whitespace_before_masking(self):
        # OCR 常带空格；先 strip 再遮，否则可见位会错位
        self.assertEqual(masking.mask_phone(" 13931298426 "), "139****8426")


class Idcard(unittest.TestCase):
    def test_standard_shape(self):
        out = masking.mask_idcard("110101199003071234")
        self.assertEqual(out, "1101**********1234")
        self.assertTrue(IDCARD_SHAPE.match(out))

    def test_star_width_is_fixed(self):
        # 星号数固定 10 颗，不随输入长度变化——否则能从长度反推位数
        for value in ("110101199003071234", "11010119900307123X"):
            with self.subTest(value=value):
                self.assertEqual(masking.mask_idcard(value).count("*"), 10)

    def test_never_returns_whole_value(self):
        for value in ("", "1", "110101", "11010119900307"):
            with self.subTest(value=value):
                out = masking.mask_idcard(value)
                if value:
                    self.assertNotEqual(out, value)
                    self.assertIn("*", out)


    def test_tiny_fragments_are_fully_masked(self):
        for value in ("1", "1101", "110101"):
            with self.subTest(value=value):
                out = masking.mask_idcard(value)
                self.assertEqual(out, "*" * len(value), out)
                self.assertFalse(any(ch.isdigit() for ch in out), out)

    def test_plausible_truncation_keeps_two(self):
        for value in ("110101199003", "1101011990030712"):
            with self.subTest(value=value):
                out = masking.mask_idcard(value)
                self.assertTrue(out.startswith(value[:2]), out)
                self.assertEqual(len(out), len(value))
                self.assertEqual(out.count("*"), len(value) - 2)


class RandomisedInvariant(unittest.TestCase):
    """随机造 300 个号码，逐个断言"原文的中间段不会出现在输出里"。"""

    def test_middle_digits_never_leak(self):
        rng = random.Random(20261102)
        for _ in range(300):
            value = "1" + "".join(rng.choice("0123456789") for _ in range(10))
            out = masking.mask_phone(value)
            with self.subTest(value=value):
                self.assertTrue(PHONE_SHAPE.match(out), out)
                # 中间 4 位（第 4-7 位）必须被遮掉
                self.assertNotIn(value[3:7], out)

    def test_idcard_middle_never_leaks(self):
        rng = random.Random(20261102)
        for _ in range(200):
            value = "".join(rng.choice("0123456789") for _ in range(17)) + rng.choice("0123456789X")
            out = masking.mask_idcard(value)
            with self.subTest(value=value):
                self.assertTrue(IDCARD_SHAPE.match(out), out)
                self.assertNotIn(value[4:14], out)


class Dispatch(unittest.TestCase):
    def test_mask_for_routes_text_types(self):
        self.assertEqual(masking.mask_for("text", "phone", "13931298426"), "139****8426")
        self.assertEqual(masking.mask_for("text", "idcard", "110101199003071234"),
                         "1101**********1234")

    def test_mask_for_summarises_non_text(self):
        # meta / qr 没有"可以露一半"的字段，只给类别摘要，不给值
        self.assertEqual(masking.mask_for("qr", "qr_wifi", "WIFI:S:Office;P:12345678;;"),
                         "qr/qr_wifi")
        self.assertEqual(masking.mask_for("meta", "gps"), "meta/gps")

    def test_unknown_text_subtype_falls_back_to_summary(self):
        # 出现未定义的 text 子类时，宁可只给摘要，也不能把原文当"非文本"放过去
        self.assertEqual(masking.mask_for("text", "plate", "京A12345"), "text/plate")


class ShapeOfDataset(unittest.TestCase):
    """真值里的 masked_preview 必须是由本模块产出的形状。"""

    DEV = ROOT / "dataset" / "dev" / "labels"

    def test_all_previews_match_shape(self):
        import json

        checked = 0
        for path in sorted(self.DEV.glob("*.json")):
            for rec in json.loads(path.read_text(encoding="utf-8")):
                if rec.get("record_type") != "Detection":
                    continue
                sub = rec.get("subtype")
                preview = (rec.get("details") or {}).get("masked_preview")
                if sub == "phone":
                    with self.subTest(image=path.stem):
                        self.assertRegex(preview or "", PHONE_SHAPE)
                    checked += 1
                elif sub == "idcard":
                    with self.subTest(image=path.stem):
                        self.assertRegex(preview or "", IDCARD_SHAPE)
                    checked += 1
        self.assertGreaterEqual(checked, 30, "样本太少，这条测试没起到守门作用")


class CountryCode(unittest.TestCase):
    """带区号的号码必须和裸号码得到**同一个**掩码。

    2026-11-20 修正：原实现对 `+8613800138000` 输出 `+86****8000`——区号露在外面，
    且与 A 引擎（先剥区号再遮）形状不同。同一号码在两处显示不一致，是红线的缺口。
    """

    def test_three_prefix_forms_collapse_to_one_shape(self):
        for value in ("+8613800138000", "008613800138000", "8613800138000"):
            with self.subTest(value=value):
                self.assertEqual(masking.mask_phone(value), "138****8000")

    def test_prefix_does_not_leak_country_code(self):
        out = masking.mask_phone("+8613931298426")
        self.assertNotIn("+", out)
        self.assertNotIn("86", out[:3], f"区号仍可见：{out}")
        self.assertEqual(out, "139****8426")

    def test_plain_number_unaffected(self):
        self.assertEqual(masking.mask_phone("13931298426"), "139****8426")


if __name__ == "__main__":
    unittest.main(verbosity=2)
