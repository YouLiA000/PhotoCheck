"""`check_env` 的版本区间必须真的比较，而不是只打印。

`PACKAGES` 里写着 `>=5,<6`、`10.x`、`1.1.3` 这类区间，但 `check_packages` 原来
只把 `want` 拼进输出、从不比较——装了 gradio 6.x 也照样报 OK，
DoD「在别人的电脑上跑通一次」的入口等于装饰。

本测试只测纯函数，不依赖本机装了什么。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import check_env  # noqa: E402


class ParseSpec(unittest.TestCase):
    def test_range_and_wildcard(self):
        self.assertEqual(
            check_env.parse_spec(">=5,<6"),
            [(">=", (5,)), ("<", (6,))],
        )
        self.assertEqual(check_env.parse_spec("10.x"), [("~", (10,))])
        self.assertEqual(check_env.parse_spec("1.1.3"), [("==", (1, 1, 3))])

    def test_blank_clauses_ignored(self):
        self.assertEqual(check_env.parse_spec(">=4.21, <5"), [(">=", (4, 21)), ("<", (5,))])


class InRange(unittest.TestCase):
    CASES = [
        ("5.9.0", ">=5,<6", True),
        ("6.0.1", ">=5,<6", False),
        ("4.99.9", ">=5,<6", False),
        ("10.4.0", "10.x", True),
        ("9.5.0", "10.x", False),
        ("1.1.3", "1.1.3", True),
        ("1.1.4", "1.1.3", False),
        ("0.18.1", ">=0.18", True),
        ("0.17.9", ">=0.18", False),
        ("0.10.14", "0.10.x", True),
        ("0.11.0", "0.10.x", False),
        ("4.21.0", ">=4.21,<5", True),
        ("5.0.0", ">=4.21,<5", False),
    ]

    def test_cases(self):
        for version, spec, expected in self.CASES:
            with self.subTest(version=version, spec=spec):
                verdict, _note = check_env.in_range(version, spec)
                self.assertEqual(verdict, expected)

    def test_unparsable_version_returns_none(self):
        verdict, note = check_env.in_range("unknown", "10.x")
        self.assertIsNone(verdict)
        self.assertTrue(note)

    def test_prerelease_is_flagged_but_passes(self):
        verdict, note = check_env.in_range("5.0.0rc1", ">=5,<6")
        self.assertTrue(verdict)
        self.assertIn("预发布", note)

    def test_two_component_version_against_three_component_spec(self):
        # "1.26" 对 "1.26.x"：补零后应视为符合
        verdict, _ = check_env.in_range("1.26.0", "1.26.x")
        self.assertTrue(verdict)


class PackagesTableIsSelfConsistent(unittest.TestCase):
    """PACKAGES 里每一行都必须能被 parse_spec 解析，别留无法判定的写法。"""

    def test_every_spec_is_parseable(self):
        for dist, _module, want, _required in check_env.PACKAGES:
            with self.subTest(package=dist):
                clauses = check_env.parse_spec(want)
                self.assertTrue(clauses, f"{dist} 的区间 `{want}` 解析为空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
