"""K-10 风险分级与解释测试。

两张表要分开看：
  - 分值表：K-04 / K-10 写死的数字，改要三人同意 → 这里逐条钉住
  - 分档：≥5 高 / 3–4 中 / ≤2 低 → 用构造样例验证边界

    python -m unittest discover -s contract/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract import risk_explain as rx  # noqa: E402
from contract.risk_rules import (  # noqa: E402
    BONUS_RULES,
    PROVISIONAL_SCORE,
    SPEC_SCORE,
    assess,
    detection_risk,
    image_severity,
    resolve_risk,
)

META = {"gps", "datetime", "device", "serial", "thumbnail", "software"}
TEXT = {"phone", "idcard"}
FACE = {"face_region"}


def det(subtype: str, **details) -> dict:
    """造一条最小合法 Detection，只填本测试关心的字段。"""
    if subtype in META:
        kind, evidence = "meta", "exif"
    elif subtype in TEXT:
        kind, evidence = "text", "ocr_rule"
    elif subtype in FACE:
        kind, evidence = "face", "face_box"
    else:
        kind, evidence = "qr", "qr_payload"
    if subtype == "idcard" and "idcard_check" not in details:
        details["idcard_check"] = "pass"
    return {
        "contract_version": "photocheck/v4",
        "record_type": "Detection",
        "id": f"det-{subtype}-1",
        "image_id": "image-test",
        "kind": kind,
        "subtype": subtype,
        "bbox": None if kind == "meta" else [10, 10, 100, 30],
        "detector_score": 0.9,
        "evidence": evidence,
        "risk": detection_risk(kind, subtype, details),
        "details": details,
    }


class Scores(unittest.TestCase):
    """K-04 / K-10 写死的分值。"""

    def test_spec_scores(self):
        cases = [
            (("meta", "gps", {}), 3),
            (("text", "phone", {}), 2),
            (("text", "idcard", {"idcard_check": "pass"}), 3),
            (("text", "idcard", {"idcard_check": "fail"}), 2),
            (("qr", "qr_wifi", {}), 3),
            (("qr", "qr_wechat_pay", {}), 3),
            (("qr", "qr_alipay", {}), 3),
            (("qr", "qr_wechat_group", {}), 3),
            (("qr", "qr_geo", {}), 3),
            (("qr", "qr_link", {}), 1),
            (("face", "face_region", {}), 1),
            (("meta", "device", {}), 1),
            (("meta", "serial", {}), 2),
        ]
        for (kind, sub, details), want in cases:
            with self.subTest(kind=kind, sub=sub, details=details):
                self.assertEqual(detection_risk(kind, sub, details), want)

    def test_unknown_subtype_raises_so_nobody_guesses(self):
        # 宁可在开发期炸，也不要在联调时给一个没人认账的分值
        with self.assertRaises(KeyError):
            detection_risk("text", "plate", {})


class Grading(unittest.TestCase):
    """K-10 分档：≥5 高 / 3–4 中 / ≤2 低。五组构造样例。"""

    def test_five_constructed_cases(self):
        cases = [
            ("单 GPS：3 分 → 中", [det("gps")], "中", 3),
            ("GPS+机型+序列号：6 分 → 高", [det("gps"), det("device"), det("serial")], "高", 6),
            ("单手机号：2 分 → 低", [det("phone")], "低", 2),
            ("Wi-Fi 码 + GPS：3+3+3 加成 → 高", [det("qr_wifi"), det("gps")], "高", 9),
            ("干净图：0 分 → 低", [], "低", 0),
        ]
        for title, dets, level, score in cases:
            with self.subTest(case=title):
                exp = rx.explain(dets)
                self.assertEqual((exp.level, exp.score), (level, score), title)

    def test_bonus_gps_plus_face(self):
        # 3 + 1 + 2 = 6，不加成就只有 4（仍是"中"）——加成把结论从"中"推到"高"
        dets = [det("gps"), det("face_region")]
        self.assertEqual(rx.explain(dets).score, 6)
        self.assertFalse(rx.explain([det("gps")]).bonuses)
        self.assertEqual(len(rx.explain(dets).bonuses), 1)

    def test_bonus_not_double_counted_for_duplicates(self):
        # 两个 GPS 是同一件事，不该触发两次加成（否则堆对象就能刷高等级）
        dets = [det("gps"), dict(det("gps"), id="det-gps-2"), det("face_region")]
        self.assertEqual(len(rx.explain(dets).bonuses), 1)

    def test_boundary_values(self):
        self.assertEqual(image_severity([det("gps"), det("serial")])[0], "高")       # 5
        self.assertEqual(image_severity([det("gps")])[0], "中")                      # 3
        self.assertEqual(image_severity([det("phone")])[0], "低")                    # 2


class Wording(unittest.TestCase):
    """文案全走模板（K-10 验收），且不许越界。"""

    def test_sentence_follows_spec_template(self):
        exp = rx.explain([det("gps"), det("serial")])
        self.assertIn("**高风险**", exp.sentence)
        self.assertIn("来源：EXIF 元数据", exp.sentence)
        self.assertIn("合计 5 分", exp.sentence)
        self.assertTrue(exp.sentence.endswith(rx.ADVICE))

    def test_every_finding_has_evidence(self):
        dets = [det("gps"), det("phone"), det("qr_wifi")]
        exp = rx.explain(dets)
        self.assertEqual(len(exp.evidence), 3)
        for name in exp.evidence:
            self.assertIn(name, rx.EVIDENCE_LABEL.values(), "来源必须是固定词表里的中文名")

    def test_score_is_never_written_as_probability(self):
        # K-10 score 纪律：只用于排序与过滤，不得写成概率
        for dets in ([det("gps")], [det("gps"), det("serial")], []):
            text = rx.panel_markdown(dets)
            self.assertNotIn("%", text)
            self.assertNotIn("概率", text)

    def test_no_safety_promise(self):
        banned = ["已经处理干净", "可以安全分享", "可以放心分享", "无隐私风险",
                  "已确保安全", "发给熟人"]
        for dets in ([], [det("gps")], [det("phone"), det("idcard")]):
            text = rx.panel_markdown(dets)
            for word in banned:
                self.assertNotIn(word, text, f"出现禁用表述：{word}")

    def test_empty_case_states_scope_not_safety(self):
        exp = rx.explain([])
        self.assertEqual((exp.level, exp.score), ("低", 0))
        self.assertIn("未检出", exp.sentence)
        self.assertIn("只覆盖", exp.sentence)


class Consistency(unittest.TestCase):
    """A 填的 risk 与规则不一致时，不能悄悄按自己的算完就算。"""

    def test_mismatch_reported_and_rule_wins(self):
        bad = det("phone")
        bad["risk"] = 1                      # 检测器填错了（应为 2）
        exp = rx.explain([bad])
        self.assertEqual(exp.score, 2, "应当以规则为准")
        self.assertEqual(exp.mismatches, [("det-phone-1", 1, 2)])

    def test_unknown_subtype_falls_back_instead_of_crashing(self):
        # A 多回了一类，界面不能整页崩掉
        odd = det("phone")
        odd["subtype"] = "plate"
        odd["risk"] = 2
        exp = rx.explain([odd])
        self.assertEqual(exp.score, 2)
        self.assertEqual(len(exp.mismatches), 0, "规则没有定义的分值谈不上不一致")

    def test_provisional_scores_are_flagged(self):
        # 拍摄时间是推定的 1 分，规格卡没写；面板必须说明，不能当成实测结论
        exp = rx.explain([det("datetime")])
        self.assertIn("datetime", exp.unconfirmed)
        self.assertIn("未经三人确认", rx.panel_markdown([det("datetime")]))


class Panel(unittest.TestCase):
    """K-12：面板只允许三块内容。"""

    def test_panel_has_only_three_blocks(self):
        text = rx.panel_markdown([det("gps"), det("phone")])
        self.assertIn("风险项计数", text)
        self.assertIn("复检结论", text)
        self.assertIn("处置有效性", text)
        self.assertIn("已知保留项与复检异常", text)

    def test_panel_counts_by_subtype_not_kind(self):
        # K-12 要求按 subtype 分组；按 kind 分会把手机号和身份证并成"文字 2 项"
        text = rx.panel_markdown([det("phone"), det("idcard")])
        self.assertIn("| 手机号 | 1 |", text)
        self.assertIn("| 身份证号 | 1 |", text)

    def test_panel_has_no_bbox_or_original_text(self):
        text = rx.panel_markdown([det("phone", masked_preview="138****0000")])
        self.assertNotIn("138****0000", text, "面板连掩码值都不需要，只给计数")
        self.assertNotIn("bbox", text)



class SingleSourceOfTruth(unittest.TestCase):
    """分只能算一次：`risk_rules` 算，`risk_explain` 说。

    这里守的是今天修掉的一处真实分歧——`image_severity` 原本拿检测器自带的 `risk` 求和，
    而面板按规则重算。只要 A 填错一个值，界面和报告就会对同一张图给出不同等级，
    且两边各自都"算对了"，联调时极难查。
    """

    def test_image_severity_agrees_with_assess(self):
        for dets in ([], [det("gps")], [det("gps"), det("serial")],
                     [det("qr_wifi"), det("gps")], [det("phone"), det("idcard")]):
            with self.subTest(n=len(dets)):
                a = assess(dets)
                level, score, _ = image_severity(dets)
                self.assertEqual((level, score), (a.level, a.score))

    def test_image_severity_ignores_declared_risk(self):
        # 检测器把手机号填成 1 分，整图仍应按规则的 2 分算
        bad = det("phone")
        bad["risk"] = 1
        self.assertEqual(image_severity([bad])[1], 2)

    def test_every_bonus_rule_has_wording_and_matches_spec(self):
        # 加成值写错比漏写更难发现：等级只是偏了一档，没人会去手算
        self.assertEqual(
            {(a, b): extra for a, b, extra in BONUS_RULES},
            {("gps", "face_region"): 2, ("gps", "qr_wifi"): 3},
        )
        for a, b, extra in BONUS_RULES:
            with self.subTest(pair=(a, b)):
                self.assertIn((a, b), rx.BONUS_LABEL, "加成有规则没文案，界面会露英文键")
                self.assertIn(f"+{extra}", rx.BONUS_LABEL[(a, b)])

    def test_bonus_subtypes_exist_in_score_tables(self):
        # 加成引用了规则表里没有的 subtype，说明两边有一处写错
        known = {sub for _, sub in SPEC_SCORE} | {sub for _, sub in PROVISIONAL_SCORE}
        for a, b, _ in BONUS_RULES:
            with self.subTest(pair=(a, b)):
                self.assertIn(a, known)
                self.assertIn(b, known)

    def test_resolve_risk_states(self):
        ok = det("phone")
        self.assertEqual(resolve_risk(ok), (2, "rule"))

        bad = dict(ok, risk=1)
        self.assertEqual(resolve_risk(bad), (2, "rule_mismatch"))

        odd = dict(ok, subtype="plate")      # 规则里没有这一类
        self.assertEqual(resolve_risk(odd), (2, "declared"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
