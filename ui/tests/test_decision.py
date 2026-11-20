"""Decision 生成与手动补框的测试（K-11 · W6/W7）。

决定是契约记录，不是界面状态：它要过 `validate_records`、要被 B 直接消费。
所以这里不测按钮，测**生成出来的记录对不对**。

    python -m unittest ui.tests.test_decision -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract.validate_records import validate_records  # noqa: E402
from ui import datasource, decision  # noqa: E402


def findings_of(sample: str = "image-dev-001") -> list[dict]:
    return datasource.detections(datasource.load_records(sample))


class Eligibility(unittest.TestCase):
    def test_meta_is_excluded(self):
        # 契约：元数据不接受 Decision，系统始终清除整图元数据
        got = decision.eligible(findings_of())
        self.assertTrue(got, "样本里应有可决定的发现项")
        self.assertTrue(all(f["kind"] != "meta" for f in got))

    def test_text_and_qr_included(self):
        kinds = {f["kind"] for f in decision.eligible(findings_of("image-dev-014"))}
        self.assertIn("text", kinds)
        self.assertIn("qr", kinds)


class BuildDecisions(unittest.TestCase):
    def setUp(self):
        self.findings = findings_of()
        self.ids = [f["id"] for f in decision.eligible(self.findings)]

    def test_first_build_is_revision_one_and_all_redact(self):
        recs = decision.build_decisions(self.findings, self.ids)
        self.assertEqual(len(recs), len(self.ids))
        for rec in recs:
            self.assertEqual(rec["action"], "redact")
            self.assertEqual(rec["revision"], 1)

    # --- 与契约流水线的关系 -------------------------------------------------
    # Decision 单独一包**过不了校验**，这不是缺陷，是契约在强制分工：
    #   ① 每条 Decision 必须能对上一条 Detection（否则 decision_orphan）
    #   ② redact 的决定必须已经有对应计划区域（否则 redact_without_plan_item）
    # ②里的计划归 B（K-08）。下面用一个测试替身把链路闭合，证明 C 的产出**可被消费**。

    def _fake_plan(self, recs):
        """模拟 B 的 RedactionPlan：只为验证 C 的输出能不能接上，不是 B 的实现。"""
        by_id = {f["id"]: f for f in self.findings}
        items = [
            {
                "id": f"redact:{r['finding_id']}",
                "finding_id": r["finding_id"],
                "bbox": by_id[r["finding_id"]]["bbox"],
                "method": "solid",
            }
            for r in recs if r["action"] == "redact"
        ]
        return {
            "contract_version": "photocheck/v4",
            "record_type": "RedactionPlan",
            "image_id": "image-dev-001",
            "revision": 1,
            "items": items,
        }

    def test_closes_the_pipeline_together_with_detections_and_plan(self):
        recs = decision.build_decisions(self.findings, self.ids)
        batch = datasource.load_records("image-dev-001") + recs + [self._fake_plan(recs)]
        report = validate_records(batch)
        self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_retain_only_needs_no_plan(self):
        # 全部保留时不该要求计划——否则"用户什么都不想遮"会被判违约
        recs = decision.build_decisions(self.findings, [])
        batch = datasource.load_records("image-dev-001") + recs
        self.assertTrue(validate_records(batch).ok, "全 retain 不该需要 RedactionPlan")

    def test_redact_without_plan_is_flagged(self):
        # 把边界钉住：redact 少了计划区域，契约会报出来，界面不能假装完成
        recs = decision.build_decisions(self.findings, self.ids)
        batch = datasource.load_records("image-dev-001") + recs
        codes = {i.code for i in validate_records(batch).errors}
        self.assertIn("redact_without_plan_item", codes)

    def test_deselect_produces_retain_and_bumps_revision_globally(self):
        """取消一项后：**所有** Decision 一起升到 2，包括动作没变的那些。

        契约要求同一份交接包共享一个 revision；只升变化项会产出
        revision 1 + 2 混合的包，B 的 validate_records 直接拒。
        """
        first = decision.build_decisions(self.findings, self.ids)
        keep = self.ids[:1]
        second = decision.build_decisions(
            self.findings, keep, previous=decision.index_decisions(first)
        )
        by_id = {r["finding_id"]: r for r in second}
        self.assertEqual(by_id[self.ids[0]]["action"], "redact")
        self.assertEqual(by_id[self.ids[0]]["revision"], 2, "没变的也要跟着升版本")
        for fid in self.ids[1:]:
            self.assertEqual(by_id[fid]["action"], "retain")
            self.assertEqual(by_id[fid]["revision"], 2, "同一份包里必须是同一个 revision")
        self.assertEqual(
            len({r["revision"] for r in second}), 1, "剩余所有 Decision 必须共享一个 revision"
        )

    def test_toggling_back_bumps_again(self):
        first = decision.build_decisions(self.findings, self.ids)
        keep = self.ids[:1]
        second = decision.build_decisions(
            self.findings, keep, previous=decision.index_decisions(first)
        )
        third = decision.build_decisions(
            self.findings, self.ids, previous=decision.index_decisions(second)
        )
        # 先 retain（改过一次 → 2），又改回 redact → 3
        self.assertEqual(third[-1]["revision"], 3)

    def test_same_input_is_idempotent(self):
        first = decision.build_decisions(self.findings, self.ids)
        again = decision.build_decisions(
            self.findings, self.ids, previous=decision.index_decisions(first)
        )
        self.assertEqual(first, again, "没改动作就不能改 revision")

    def test_one_decision_per_finding(self):
        recs = decision.build_decisions(self.findings, self.ids)
        self.assertEqual(len(recs), len({r["finding_id"] for r in recs}))

    def test_duplicate_decisions_are_rejected(self):
        recs = decision.build_decisions(self.findings, self.ids)
        with self.assertRaises(ValueError):
            decision.index_decisions(recs + [dict(recs[0])])

    def test_empty_selection_retains_everything(self):
        recs = decision.build_decisions(self.findings, [])
        self.assertTrue(all(r["action"] == "retain" for r in recs))

    def test_summary_counts(self):
        recs = decision.build_decisions(self.findings, self.ids[:1])
        got = decision.summarize(recs)
        self.assertEqual(got["redact"], 1)
        self.assertEqual(got["retain"], len(self.ids) - 1)
        self.assertEqual(got["total"], len(self.ids))


class ManualBox(unittest.TestCase):
    def test_id_is_sequential_and_marked(self):
        box = decision.make_manual_box(10, 20, 100, 50, width=800, height=600, seq=7)
        self.assertEqual(box["id"], "manual-007")
        self.assertEqual(box["bbox"], [10, 20, 100, 50])
        self.assertEqual(box["method"], "solid")

    def test_out_of_bounds_is_clamped(self):
        # 越界框原样交给处置器会切出负数索引
        box = decision.make_manual_box(-30, -10, 500, 500, width=200, height=200, seq=1)
        self.assertEqual(box["bbox"], [0, 0, 200, 200])

    def test_negative_origin_shrinks_instead_of_shifting(self):
        """起点在负方向越界时，保留的是**实际框住的那部分**。

        从 -5 起、宽 100 的框，图上覆盖 0..95，宽应是 95（再被图宽截到 80），
        而不是"x 归零、宽度不动 100"。后者会多遮 5px，而第三层评测量的正是误遮。
        """
        box = decision.make_manual_box(-5, -5, 100, 50, width=80, height=60, seq=1)
        self.assertEqual(box["bbox"], [0, 0, 80, 45])

    def test_partially_outside_keeps_visible_width(self):
        # 大图上的纯左侧越界：x=-50,w=100 → 可见 0..50，宽 50
        self.assertEqual(decision.clamp_bbox(-50, 10, 100, 30, width=1000, height=1000),
                         [0, 10, 50, 30])

    def test_degenerate_box_rejected(self):
        for bad in ((10, 10, 2, 50), (10, 10, 50, 1)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    decision.make_manual_box(*bad, width=800, height=600, seq=1)

    def test_non_integer_rejected(self):
        with self.assertRaises(ValueError):
            decision.make_manual_box("x", 0, 100, 100, width=800, height=600, seq=1)

    def test_unknown_method_rejected(self):
        with self.assertRaises(ValueError):
            decision.make_manual_box(0, 0, 100, 100, width=800, height=600, seq=1, method="pixelate")

    def test_box_fully_outside_still_fails(self):
        # 钳完不到 3×3，说明坐标根本落在图外，不能默默放过
        with self.assertRaises(ValueError):
            decision.clamp_bbox(2000, 2000, 100, 100, width=800, height=600)


class Export(unittest.TestCase):
    def test_payload_separates_decisions_from_boxes(self):
        findings = findings_of()
        ids = [f["id"] for f in decision.eligible(findings)]
        recs = decision.build_decisions(findings, ids)
        box = decision.make_manual_box(10, 10, 100, 100, width=800, height=600, seq=1)
        payload = decision.export_payload("image-dev-001", recs, [box])
        self.assertEqual(payload["image_id"], "image-dev-001")
        self.assertTrue(all(r["record_type"] == "Decision" for r in payload["decisions"]))
        # 补框不是 Detection，也不该混进 decisions
        self.assertEqual(len(payload["manual_boxes"]), 1)
        self.assertNotIn("record_type", payload["manual_boxes"][0])

    def test_payload_never_contains_raw_text(self):
        import re

        findings = findings_of()
        ids = [f["id"] for f in decision.eligible(findings)]
        payload = decision.export_payload(
            "image-dev-001", decision.build_decisions(findings, ids), []
        )
        text = str(payload)
        self.assertIsNone(re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text))
        self.assertIsNone(re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", text))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CrossSampleGuard(unittest.TestCase):
    """换样本后导出不能串图。

    真实事故路径：在 001 上生成决定 → 切到 009 → 直接点导出（没重新生成）。
    导出读的是 State，于是 payload 里 image_id 是 009、decisions 却是 001 的。
    B 拿着它按 009 的尺寸去涂 001 的框，payload 本身还完全合法——
    **错误数据比报错难发现得多**。
    """

    def test_foreign_decisions_are_dropped(self):
        from_001 = decision.build_decisions(findings_of("image-dev-001"),
                                            [f["id"] for f in decision.eligible(findings_of("image-dev-001"))])
        kept, dropped = decision.filter_for_findings(from_001, findings_of("image-dev-009"))
        self.assertEqual(kept, [])
        self.assertEqual(dropped, len(from_001))

    def test_same_sample_keeps_everything(self):
        findings = findings_of("image-dev-001")
        recs = decision.build_decisions(findings, [f["id"] for f in decision.eligible(findings)])
        kept, dropped = decision.filter_for_findings(recs, findings)
        self.assertEqual(len(kept), len(recs))
        self.assertEqual(dropped, 0)

    def test_retain_decisions_also_filtered(self):
        # 只保留的决定同样不能串到别的图
        findings = findings_of("image-dev-001")
        recs = decision.build_decisions(findings, [])
        kept, dropped = decision.filter_for_findings(recs, findings_of("image-dev-021"))
        self.assertEqual((kept, dropped), ([], len(recs)))


class BoxesRevision(unittest.TestCase):
    """补框版次的语义契约。

    B 用这个字段判断"这份包和上一份是不是同一批框"。它必须是**内容指纹**而不是计数器：
    计数器存在会话内存里，重启归零，B 会看到版本号倒退并误判成更旧的包。
    """

    def test_same_boxes_same_revision(self):
        a = [{"id": "manual-001", "bbox": [10, 10, 50, 50], "method": "solid"}]
        b = [{"id": "manual-002", "bbox": [10, 10, 50, 50], "method": "solid"}]
        self.assertEqual(decision.boxes_revision(a), decision.boxes_revision(b),
                         "删掉再加回来只是换了 id，计划区域没变，不该算新批次")

    def test_moving_one_pixel_changes_revision(self):
        a = decision.boxes_revision([{"bbox": [10, 10, 50, 50], "method": "solid"}])
        b = decision.boxes_revision([{"bbox": [10, 11, 50, 50], "method": "solid"}])
        self.assertNotEqual(a, b, "坐标变了必须换版次，否则复检报告会对错区域")

    def test_changing_method_changes_revision(self):
        a = decision.boxes_revision([{"bbox": [10, 10, 50, 50], "method": "solid"}])
        b = decision.boxes_revision([{"bbox": [10, 10, 50, 50], "method": "mosaic"}])
        self.assertNotEqual(a, b, "同区域换遮盖手法，遮盖效果变了，算新批次")

    def test_order_does_not_matter(self):
        one = {"bbox": [10, 10, 50, 50], "method": "solid"}
        two = {"bbox": [90, 90, 20, 20], "method": "solid"}
        self.assertEqual(decision.boxes_revision([one, two]),
                         decision.boxes_revision([two, one]))

    def test_empty_boxes_is_stable(self):
        """空补框也要有个确定值——B 得能区分"没补框"和"字段缺失"。"""
        self.assertTrue(decision.boxes_revision([]).startswith("sha256:"))
        self.assertEqual(decision.boxes_revision([]), decision.boxes_revision([]))

    def test_payload_carries_the_field(self):
        payload = decision.export_payload("image-dev-001", [], [{"bbox": [1, 2, 3, 4]}])
        self.assertIn("manual_boxes_revision", payload)
        self.assertEqual(payload["manual_boxes_revision"],
                         decision.boxes_revision(payload["manual_boxes"]))
