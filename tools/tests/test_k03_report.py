"""K-03 报告模板与校验器的单元测试。

不依赖 A 的引擎，只验「口径算得对不对、字段查得全不全」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import k03_report  # noqa: E402


def samples() -> dict[str, list[float]]:
    """五张图，每图三次稳态耗时；图内中位数依次 20/50/80/110/140。"""
    return {
        "R1": [10.0, 20.0, 30.0],
        "R2": [40.0, 50.0, 60.0],
        "R3": [70.0, 80.0, 90.0],
        "R4": [100.0, 110.0, 120.0],
        "R5": [130.0, 140.0, 150.0],
    }


def valid_report() -> dict:
    agg = k03_report.compute_aggregates(samples())
    return {
        "data_version": k03_report.DATA_VERSION,
        "generated_at": "2026-11-16T00:00:00+08:00",
        "cpu": "Intel i5-12400",
        "power_state": "AC",
        "method": {
            "process_warmup_runs": 1,
            "per_image_discard_runs": 1,
            "per_image_repeats": 3,
        },
        "generator_command": "python gen_ref.py --seed 0",
        "generator_seed": 0,
        "reference_set": [
            {
                "id": rid,
                "width": width,
                "height": height,
                "content": content,
                "drawn_chars": drawn,
                "sha256": "a" * 64,
                "ocr_chars": drawn - 1,
            }
            for rid, width, height, content, drawn in k03_report.REFERENCE_SET
        ],
        "images": [
            {
                "id": rid,
                "samples_ms": vals,
                "median_ms": sorted(vals)[1],
                "range_ms": max(vals) - min(vals),
            }
            for rid, vals in samples().items()
        ],
        "aggregates": agg,
        "pixel_median": 0.55,
        "ocr_chars_median": 95,
        "t_ocr_ref_v2": agg["median_of_image_medians"],
        "cold_start_ms": 5200.0,
    }


class Aggregates(unittest.TestCase):
    def test_median_p95_max_and_n(self):
        agg = k03_report.compute_aggregates(samples())
        self.assertEqual(agg["median_of_image_medians"], 80.0)
        # 五图中位 20/50/80/110/140，线性插值 p95 = 110 + 0.8×30 = 134.0
        # （最邻近秩会给 140.0；2026-11-16 起与 A 的 bench_ocr.py 对齐用线性插值）
        self.assertEqual(agg["p95_of_image_medians"], 134.0)
        self.assertEqual(agg["max_of_image_medians"], 140.0)
        self.assertEqual(agg["n_images"], 5)

    def test_median_of_intra_image_ranges(self):
        agg = k03_report.compute_aggregates(samples())
        # 每图极差都是 20 → 中位数 20
        self.assertEqual(agg["median_of_intra_image_ranges"], 20.0)

    def test_bootstrap_ci_present_when_n_below_30(self):
        agg = k03_report.compute_aggregates(samples())  # n=5 < 30
        lo, hi = agg["bootstrap_95_ci_p95"]
        self.assertLessEqual(lo, hi)

    def test_rejects_fewer_than_3_samples(self):
        with self.assertRaises(ValueError):
            k03_report.compute_aggregates({"R1": [10.0, 20.0]})

    def test_percentile_defaults_to_linear_like_a_engine(self):
        # A 的 bench_ocr.py 用线性插值；2026-11-16 用其真实报告中五位复算验证
        self.assertAlmostEqual(k03_report.percentile([1, 2, 3, 4, 5], 0.95), 4.8)
        self.assertEqual(k03_report.percentile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertAlmostEqual(
            k03_report.percentile([976.9, 2690.2, 1388.2, 2822.5, 3162.3], 0.95),
            3094.4,
            delta=0.1,  # A 的报告只保留 1 位小数（实算 3094.34）
        )

    def test_percentile_nearest_rank_is_opt_in(self):
        self.assertEqual(
            k03_report.percentile([1, 2, 3, 4, 5], 0.95, method="nearest_rank"), 5.0
        )
        self.assertEqual(
            k03_report.percentile(
                [976.9, 2690.2, 1388.2, 2822.5, 3162.3], 0.95, method="nearest_rank"
            ),
            3162.3,
        )

    def test_unknown_percentile_method_rejected(self):
        with self.assertRaises(ValueError):
            k03_report.percentile([1, 2, 3], 0.95, method="cubic")


class Validate(unittest.TestCase):
    def test_complete_report_passes(self):
        self.assertEqual(k03_report.validate_report(valid_report()), [])

    def test_missing_top_level_field_reported(self):
        report = valid_report()
        del report["t_ocr_ref_v2"]
        problems = k03_report.validate_report(report)
        self.assertTrue(any("t_ocr_ref_v2" in p for p in problems), problems)

    def test_wrong_data_version_reported(self):
        report = valid_report()
        report["data_version"] = "k03_ref_v0"
        problems = k03_report.validate_report(report)
        self.assertTrue(any("data_version" in p for p in problems), problems)

    def test_bootstrap_required_below_30(self):
        report = valid_report()
        del report["aggregates"]["bootstrap_95_ci_p95"]
        problems = k03_report.validate_report(report)
        self.assertTrue(any("bootstrap_95_ci_p95" in p for p in problems), problems)

    def test_non_ac_power_flagged(self):
        report = valid_report()
        report["power_state"] = "battery, 80%"
        problems = k03_report.validate_report(report)
        self.assertTrue(any("power_state" in p for p in problems), problems)

    def test_short_samples_reported(self):
        report = valid_report()
        report["images"][0]["samples_ms"] = [10.0, 20.0]
        problems = k03_report.validate_report(report)
        self.assertTrue(any("稳态样本" in p for p in problems), problems)

    def test_bad_sha256_reported(self):
        report = valid_report()
        report["reference_set"][0]["sha256"] = "not-a-sha"
        problems = k03_report.validate_report(report)
        self.assertTrue(any("sha256" in p for p in problems), problems)

    def test_n_images_mismatch_reported(self):
        report = valid_report()
        report["aggregates"]["n_images"] = 4
        problems = k03_report.validate_report(report)
        self.assertTrue(any("n_images" in p for p in problems), problems)

    def test_template_carries_all_top_level_fields(self):
        tpl = k03_report.build_template()
        for field in k03_report.REQUIRED_TOP_LEVEL:
            self.assertIn(field, tpl, field)


class ValidateConsistency(unittest.TestCase):
    """「字段齐全但自相矛盾」的报告不应被判有效。

    这类漏检比缺字段更危险：报告看着完整，数字却是自己写的。
    2026-11-16 实测六种矛盾全部静默通过，故补此组用例。
    """

    def setUp(self):
        self.report = valid_report()

    def test_unfilled_template_rejected(self):
        tpl = k03_report.build_template()
        for entry in tpl["reference_set"]:
            entry["sha256"] = "a" * 64
        problems = k03_report.validate_report(tpl)
        self.assertTrue(any("非正耗时" in p for p in problems), problems)

    def test_t_ocr_ref_v2_must_match_aggregates(self):
        self.report["t_ocr_ref_v2"] = (
            self.report["aggregates"]["median_of_image_medians"] + 500
        )
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("t_ocr_ref_v2" in p for p in problems), problems)

    def test_median_ms_must_match_samples(self):
        self.report["images"][0]["median_ms"] = 5.0
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("median_ms" in p for p in problems), problems)

    def test_range_ms_must_match_samples(self):
        self.report["images"][0]["range_ms"] = 0.0
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("range_ms" in p for p in problems), problems)

    def test_image_ids_must_match_reference_set(self):
        self.report["images"][0]["id"] = "X1"
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("参考集" in p for p in problems), problems)

    def test_missing_reference_image_rejected(self):
        del self.report["images"][-1]
        self.report["aggregates"]["n_images"] = 4
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("缺少参考集" in p for p in problems), problems)

    def test_negative_samples_rejected(self):
        self.report["images"][0]["samples_ms"] = [-10.0, 20.0, 30.0]
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("非正耗时" in p for p in problems), problems)

    def test_duplicate_image_id_rejected(self):
        self.report["images"][1]["id"] = self.report["images"][0]["id"]
        problems = k03_report.validate_report(self.report)
        self.assertTrue(any("重复" in p for p in problems), problems)

    def test_consistent_report_still_passes(self):
        self.assertEqual(k03_report.validate_report(self.report), [])


def a_report() -> dict:
    """构造 A 的 rows/summary 形态；数值取自其 2026-11-15 真实报告。"""
    medians = [976.9, 2690.2, 1388.2, 2822.5, 3162.3]
    return {
        "version": k03_report.DATA_VERSION,
        "generated_at": "2026-11-15T23:53:01+08:00",
        "reference_manifest_sha256": "a" * 64,
        "host": {"cpu": "Intel(R) Core(TM) i7-14650HX", "power": "ac"},
        "config": {"runs_per_image": 3, "discarded_runs_per_image": 1},
        "summary": {
            "median_of_image_medians_ms": 2690.2,
            "p95_of_image_medians_ms": 3094.4,
            "max_of_image_medians_ms": 3162.3,
            "n_images": 5,
            "p95_bootstrap_ci_ms": [2429.8, 3162.3],
        },
        "rows": [
            {"id": rid, "image_median_ms": m}
            for rid, m in zip(["R1", "R2", "R3", "R4", "R5"], medians)
        ],
    }


class AFormat(unittest.TestCase):
    """A 现有产出（`make_k03_reference.py`）的形态与模板不同，要能单独校验。"""

    def test_detects_a_format(self):
        self.assertTrue(k03_report.is_a_format(a_report()))
        self.assertFalse(k03_report.is_a_format(valid_report()))

    def test_consistent_a_report_passes(self):
        self.assertEqual(k03_report.validate_a_format(a_report()), [])

    def test_p95_using_nearest_rank_is_caught(self):
        rep = a_report()
        rep["summary"]["p95_of_image_medians_ms"] = 3162.3  # 最邻近秩的结果
        problems = k03_report.validate_a_format(rep)
        self.assertTrue(any("p95" in p for p in problems), problems)

    def test_median_mismatch_is_caught(self):
        rep = a_report()
        rep["summary"]["median_of_image_medians_ms"] = 1000.0
        problems = k03_report.validate_a_format(rep)
        self.assertTrue(any("median_of_image_medians_ms" in p for p in problems), problems)

    def test_rows_id_mismatch_is_caught(self):
        rep = a_report()
        rep["rows"][0]["id"] = "X1"
        problems = k03_report.validate_a_format(rep)
        self.assertTrue(any("图 id" in p for p in problems), problems)

    def test_battery_power_flagged(self):
        rep = a_report()
        rep["host"]["power"] = "battery"
        problems = k03_report.validate_a_format(rep)
        self.assertTrue(any("power" in p for p in problems), problems)

    def test_n_images_mismatch_is_caught(self):
        rep = a_report()
        rep["summary"]["n_images"] = 4
        problems = k03_report.validate_a_format(rep)
        self.assertTrue(any("n_images" in p for p in problems), problems)

    def test_template_gaps_mention_samples(self):
        names = " ".join(name for name, _ in k03_report.TEMPLATE_ONLY_FIELDS)
        self.assertIn("samples_ms", names)
        self.assertIn("sha256", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
