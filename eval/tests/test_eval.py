"""评测逻辑测试（K-13 第一层）。

核心一条：**真值当预测必须全 1.0**。其余用例都是在故意破坏它，看指标有没有按预期动。

    python -m unittest discover -s eval/tests -v
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval import loader, metrics  # noqa: E402
from eval.matcher import iou, match_image  # noqa: E402

DEV = ROOT / "dataset" / "dev"
LOCK = ROOT / "dataset" / "lock"


def det(subtype, bbox, kind="text", risk=2):
    return {
        "contract_version": "photocheck/v4",
        "record_type": "Detection",
        "id": f"det-{subtype}",
        "image_id": "img-1",
        "kind": kind,
        "subtype": subtype,
        "bbox": bbox,
        "detector_score": 0.9,
        "evidence": {"meta": "exif", "text": "ocr_rule", "qr": "qr_payload"}[kind],
        "risk": risk,
        "details": {"masked_preview": "138****0000"} if kind == "text" else {},
    }


class IoU(unittest.TestCase):
    def test_identical_boxes(self):
        self.assertEqual(iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)

    def test_half_overlap(self):
        self.assertAlmostEqual(iou([0, 0, 10, 10], [5, 0, 10, 10]), 1 / 3, places=4)

    def test_no_overlap(self):
        self.assertEqual(iou([0, 0, 10, 10], [100, 100, 10, 10]), 0.0)


class Matching(unittest.TestCase):
    def test_perfect_match(self):
        truth = [det("phone", [10, 10, 100, 30])]
        r = match_image("img-1", truth, copy.deepcopy(truth))
        self.assertEqual((r.true_positives, r.misses, r.false_positives), (1, 0, 0))

    def test_missing_prediction(self):
        truth = [det("phone", [10, 10, 100, 30]), det("idcard", [10, 60, 180, 30])]
        r = match_image("img-1", truth, [truth[0]])
        self.assertEqual(r.true_positives, 1)
        self.assertEqual(r.misses, 1)

    def test_shifted_box_becomes_miss(self):
        truth = [det("phone", [10, 10, 100, 30])]
        pred = [det("phone", [10, 200, 100, 30])]
        r = match_image("img-1", truth, pred)
        self.assertEqual(r.misses, 1)
        self.assertEqual(r.false_positives, 1)

    def test_wrong_subtype_is_not_a_miss(self):
        # 框对上、类别判错 —— 记 subtype_error，既不是漏检也不是误报
        truth = [det("phone", [10, 10, 100, 30])]
        pred = [det("idcard", [10, 10, 100, 30])]
        r = match_image("img-1", truth, pred)
        self.assertEqual((r.true_positives, r.misses, r.false_positives), (0, 0, 0))
        self.assertEqual(r.subtype_errors, 1)

    def test_one_prediction_cannot_cover_two_truths(self):
        truth = [det("phone", [10, 10, 100, 30]), det("phone", [10, 12, 100, 30])]
        pred = [det("phone", [10, 10, 100, 30])]
        r = match_image("img-1", truth, pred)
        self.assertEqual(r.true_positives, 1)
        self.assertEqual(r.misses, 1)

    def test_false_positive_inside_negative_region(self):
        truth = []
        pred = [det("phone", [50, 50, 100, 30])]
        negatives = [{"bbox": [40, 40, 200, 60], "note": "订单号"}]
        r = match_image("img-1", truth, pred, negatives)
        self.assertEqual(r.false_positives, 1)
        self.assertEqual(r.negative_hits, 1)

    def test_meta_matched_by_subtype(self):
        truth = [det("gps", None, kind="meta", risk=3)]
        r = match_image("img-1", truth, copy.deepcopy(truth))
        self.assertEqual(r.meta_true_positives, 1)

    def test_extra_meta_is_false_positive(self):
        truth = [det("gps", None, kind="meta", risk=3)]
        pred = truth + [det("datetime", None, kind="meta", risk=1)]
        r = match_image("img-1", truth, pred)
        self.assertEqual(r.meta_true_positives, 1)
        self.assertEqual(r.meta_false_positives, 1)


class Metrics(unittest.TestCase):
    def test_subtype_error_does_not_destroy_precision(self):
        truth = [det("phone", [10, 10, 100, 30])]
        pred = [det("idcard", [10, 10, 100, 30])]
        s = metrics.summarize([match_image("img-1", truth, pred)])
        self.assertEqual(s["fp"], 0)
        self.assertEqual(s["subtype_errors"], 1)
        self.assertEqual(s["precision"], 0.0)
        self.assertEqual(s["recall"], 0.0)

    def test_small_sample_flagged(self):
        truth = [det("phone", [10, 10, 100, 30])]
        s = metrics.summarize([match_image("img-1", truth, copy.deepcopy(truth))])
        self.assertTrue(s["small_sample"])
        self.assertEqual(s["by_subtype"][0]["note"], "仅作开发参考")


class RealDataset(unittest.TestCase):
    """拿真的数据集跑：完美预测必须是 1.0，这条不过说明评测代码自己坏了。"""

    def _perfect(self, dataset_dir: Path):
        truth = loader.load_set(dataset_dir)
        pred = loader.perfect_predictions(truth)
        negatives = loader.load_negatives(dataset_dir)
        results = [
            match_image(image_id, loader.detections(records),
                        loader.detections(pred[image_id]), negatives.get(image_id, []))
            for image_id, records in truth.items()
        ]
        return metrics.summarize(results)

    def test_dev_perfect_scores_one(self):
        s = self._perfect(DEV)
        self.assertEqual(s["recall"], 1.0)
        self.assertEqual(s["precision"], 1.0)
        self.assertEqual((s["fn"], s["fp"], s["subtype_errors"]), (0, 0, 0))
        self.assertGreaterEqual(s["objects_truth"], 60)

    def test_lock_perfect_scores_one(self):
        s = self._perfect(LOCK)
        self.assertEqual(s["recall"], 1.0, "lock 集上自测不为 1，匹配逻辑有问题")
        self.assertEqual(s["precision"], 1.0)


class Reports(unittest.TestCase):
    def test_three_tables_written(self):
        from eval import reporter

        truth = loader.load_set(DEV)
        results = [
            match_image(image_id, loader.detections(records),
                        loader.detections(loader.perfect_predictions({image_id: records})[image_id]))
            for image_id, records in truth.items()
        ]
        summary = metrics.summarize(results)
        with tempfile.TemporaryDirectory() as tmp:
            paths = reporter.write_reports(
                Path(tmp), summary, metrics.layer2_placeholder(),
                metrics.layer3_placeholder(),
                {"dataset": "dev", "prediction": "test", "iou_threshold": 0.5,
                 "generated_at": "now", "git_sha": "test"},
            )
            for key in ("detection", "validity", "over", "readme", "json"):
                self.assertTrue(paths[key].exists(), key)
            head = paths["detection"].read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertIn("subtype", head)
            self.assertIn("n", head)
            text = paths["readme"].read_text(encoding="utf-8")
            self.assertIn("第一层", text)
            self.assertIn("类别判错", text)




class PredictionGuards(unittest.TestCase):
    """预测不合契约时必须当场报错，不能算成"全漏检"。

    这是 W5 最容易踩的坑：A 的输出少一个字段，评测照跑，召回变 0，
    两个人去查算法，最后发现是字段名写错。
    """

    def _write(self, payload, name="image-dev-001.json"):
        import json, tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return d

    def test_missing_required_field_is_rejected(self):
        from eval import loader

        rec = {"contract_version": "photocheck/v4", "record_type": "Detection",
               "id": "x", "image_id": "image-dev-001", "kind": "text",
               "bbox": [1, 2, 3, 4], "detector_score": 0.9, "evidence": "ocr_rule",
               "risk": 2, "details": {"masked_preview": "138****0000"}}
        d = self._write([rec])
        with self.assertRaises(ValueError) as ctx:
            loader.load_predictions(d)
        self.assertIn("subtype", str(ctx.exception))

    def test_raw_text_in_prediction_is_rejected(self):
        from eval import loader

        rec = {"contract_version": "photocheck/v4", "record_type": "Detection",
               "id": "x", "image_id": "image-dev-001", "kind": "text", "subtype": "phone",
               "bbox": [1, 2, 3, 4], "detector_score": 0.9, "evidence": "ocr_rule",
               "risk": 2, "details": {"masked_preview": "13800138000"}}
        d = self._write([rec])
        with self.assertRaises(ValueError):
            loader.load_predictions(d)

    def test_validate_can_be_disabled_for_debugging(self):
        from eval import loader

        d = self._write([{"record_type": "Detection", "image_id": "image-dev-001"}])
        got = loader.load_predictions(d, validate=False)
        self.assertIn("image-dev-001", got)

    def test_broken_json_names_the_file(self):
        import tempfile
        from pathlib import Path
        from eval import loader

        d = Path(tempfile.mkdtemp())
        (d / "image-dev-009.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            loader.load_predictions(d)
        self.assertIn("image-dev-009.json", str(ctx.exception))




class Uncertainty(unittest.TestCase):
    """置信区间与分层：把"样本太小"和"靠简单图撑起来的"这两件事摆到明面上。"""

    @staticmethod
    def _result(image_id, tp=0, fn=0, fp=0, se=0):
        from eval.matcher import ImageResult

        return ImageResult(image_id=image_id, true_positives=tp, misses=fn,
                           false_positives=fp, subtype_errors=se)

    def test_ci_brackets_point_estimate(self):
        from eval.matcher import ImageResult
        from eval import metrics

        # 10 张图，其中 2 张各漏 1 个 → 点估计 0.9
        results = [ImageResult(image_id=f"i{i}", true_positives=9) for i in range(8)]
        results += [ImageResult(image_id="bad1", true_positives=8, misses=1),
                    ImageResult(image_id="bad2", true_positives=8, misses=1)]
        summary = metrics.summarize(results)
        ci = metrics.bootstrap_ci(results, n_boot=400, seed=1)
        lo, hi = ci["recall"]
        self.assertLessEqual(lo, summary["recall"])
        self.assertGreaterEqual(hi, summary["recall"])
        # 有 2 张坏图时，重采样到"全好"是可能的，所以上界会顶到 1.0
        self.assertEqual(hi, 1.0)

    def test_ci_is_deterministic_given_seed(self):
        from eval import metrics

        results = [self._result("a", tp=3), self._result("b", tp=2, fn=1)]
        first = metrics.bootstrap_ci(results, n_boot=200, seed=42)
        second = metrics.bootstrap_ci(results, n_boot=200, seed=42)
        self.assertEqual(first, second, "同种子必须同样的区间，否则报告不可复现")

    def test_ci_empty_dataset(self):
        from eval import metrics

        ci = metrics.bootstrap_ci([])
        self.assertEqual(ci["recall"], [None, None])

    def test_perfect_prediction_ci_is_degenerate(self):
        from eval import metrics

        results = [self._result(f"i{i}", tp=5) for i in range(5)]
        self.assertEqual(metrics.bootstrap_ci(results, n_boot=100)["recall"], [1.0, 1.0])

    def test_difficulty_split(self):
        from eval import metrics

        results = [self._result("e1", tp=5), self._result("h1", tp=3, fn=1),
                   self._result("x1", tp=1)]
        rows = {r["difficulty"]: r for r in metrics.by_difficulty(
            results, {"e1": "easy", "h1": "hard"})}
        self.assertEqual(rows["easy"]["n"], 5)
        self.assertEqual(rows["hard"]["recall"], 0.75)
        self.assertIn("unknown", rows, "台账里没有难度的图要单独成桶，不要偷偷并进 easy")


class Traceability(unittest.TestCase):
    """失败必须能追到具体对象——只给计数，A 没法修。"""

    def test_failures_match_counts(self):
        from eval.matcher import match_image

        truth = [
            {"id": "t1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 30]},
            {"id": "t2", "kind": "text", "subtype": "idcard", "bbox": [0, 50, 100, 30]},
        ]
        pred = [
            {"id": "p1", "kind": "text", "subtype": "phone", "bbox": [0, 50, 100, 30]},  # 判错
            {"id": "p2", "kind": "text", "subtype": "phone", "bbox": [500, 500, 100, 30]},  # 误报
        ]
        r = match_image("img", truth, pred)
        self.assertEqual((r.misses, r.false_positives, r.subtype_errors), (1, 1, 1))
        self.assertEqual(len(r.failures), 3)
        kinds = sorted(f["kind"] for f in r.failures)
        self.assertEqual(kinds, ["false_positive", "miss", "subtype_error"])
        for f in r.failures:
            for key in ("kind", "image_id", "truth_subtype", "bbox"):
                self.assertIn(key, f)
            self.assertEqual(f["image_id"], "img")

    def test_subtype_error_records_both_sides(self):
        from eval.matcher import match_image

        truth = [{"id": "t1", "kind": "text", "subtype": "idcard", "bbox": [0, 0, 100, 30]}]
        pred = [{"id": "p1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 30]}]
        r = match_image("img", truth, pred)
        f = r.failures[0]
        self.assertEqual((f["truth_subtype"], f["pred_subtype"]), ("idcard", "phone"))
        self.assertGreaterEqual(f["iou"], 0.99)

    def test_negative_hit_is_marked(self):
        from eval.matcher import match_image

        pred = [{"id": "p1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 30]}]
        r = match_image("img", [], pred, [{"bbox": [0, 0, 200, 60]}])
        self.assertEqual(r.negative_hits, 1)
        self.assertEqual(r.failures[0]["in_negative"], True)




class Sensitivity(unittest.TestCase):
    """敏感性分析：主口径不动，另给一档宽口径看结论稳不稳。"""

    TRUTH = [{"id": "t1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 20]}]

    def test_loose_threshold_only_affects_text(self):
        from eval.matcher import iou, match_image

        # IoU ≈ 0.43：主口径判不中，0.3 档判中
        pred = [{"id": "p1", "kind": "text", "subtype": "phone", "bbox": [0, 8, 100, 20]}]
        self.assertAlmostEqual(iou(self.TRUTH[0]["bbox"], pred[0]["bbox"]), 0.4286, places=3)
        self.assertEqual(match_image("i", self.TRUTH, pred).true_positives, 0)
        loose = match_image("i", self.TRUTH, pred, thresholds={"text": 0.3})
        self.assertEqual(loose.true_positives, 1)

    def test_loose_threshold_does_not_touch_qr(self):
        from eval.matcher import match_image

        truth = [{"id": "t1", "kind": "qr", "subtype": "qr_wifi", "bbox": [0, 0, 100, 20]}]
        pred = [{"id": "p1", "kind": "qr", "subtype": "qr_wifi", "bbox": [0, 8, 100, 20]}]
        r = match_image("i", truth, pred, thresholds={"text": 0.3})
        self.assertEqual(r.true_positives, 0, "二维码是真矩形，宽口径不该放过它")

    def test_report_has_appendix_when_loose_present(self):
        from eval import reporter

        summary = {
            "images": 1, "objects_truth": 1, "objects_pred": 1,
            "tp": 1, "fn": 0, "fp": 0, "subtype_errors": 0,
            "recall": 1.0, "precision": 1.0, "fp_per_image": 0.0,
            "negative_hits": 0, "by_subtype": [], "small_sample": True,
            "by_difficulty": [], "ci": None,
            "loose": {"threshold": 0.3, "recall": 1.0, "precision": 1.0,
                      "tp": 1, "fn": 0, "fp": 0, "subtype_errors": 0},
        }
        text = reporter._render_markdown(
            summary, {"dataset": "dev", "prediction": "x", "iou_threshold": 0.5,
                      "generated_at": "now", "git_sha": "abc"}
        )
        self.assertIn("敏感性分析", text)
        self.assertIn("宽口径 0.3", text)
        self.assertIn("主结论只认主口径", text)




class Alignment(unittest.TestCase):
    """预测与真值的对齐：对不上时必须报错，不能出 0 分。

    这是 W5 最贵的一类错：A 的输出 image_id 拼错，评测照跑、指标 0、
    两个人去查算法，最后发现是字符串写错。0 分看起来像"检测器完全失效"，
    会把人带到完全错的方向。
    """

    def _write(self, name, image_id):
        import json, tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        rec = {"contract_version": "photocheck/v4", "record_type": "Detection",
               "id": f"det-{image_id}", "image_id": image_id, "kind": "text",
               "subtype": "phone", "bbox": [1, 2, 30, 12], "detector_score": 0.9,
               "evidence": "ocr_rule", "risk": 2,
               "details": {"masked_preview": "138****0000"}}
        (d / name).write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
        return d

    def test_filename_must_match_image_id(self):
        from eval import loader

        d = self._write("foo.json", "image-dev-001")
        with self.assertRaises(ValueError) as ctx:
            loader.load_predictions(d)
        msg = str(ctx.exception)
        self.assertIn("foo", msg)
        self.assertIn("image-dev-001", msg)

    def test_file_with_mixed_images_rejected(self):
        import json, tempfile
        from pathlib import Path
        from eval import loader

        d = Path(tempfile.mkdtemp())
        recs = [
            {"contract_version": "photocheck/v4", "record_type": "Detection",
             "id": "a", "image_id": "image-dev-001", "kind": "text", "subtype": "phone",
             "bbox": [1, 2, 30, 12], "detector_score": 0.9, "evidence": "ocr_rule",
             "risk": 2, "details": {"masked_preview": "138****0000"}},
            {"contract_version": "photocheck/v4", "record_type": "Detection",
             "id": "b", "image_id": "image-dev-002", "kind": "text", "subtype": "phone",
             "bbox": [1, 2, 30, 12], "detector_score": 0.9, "evidence": "ocr_rule",
             "risk": 2, "details": {"masked_preview": "138****0000"}},
        ]
        (d / "image-dev-001.json").write_text(json.dumps(recs), encoding="utf-8")
        with self.assertRaises(ValueError):
            loader.load_predictions(d)

    def test_run_rejects_foreign_predictions(self):
        from pathlib import Path
        from eval import eval as runner

        with self.assertRaises(ValueError) as ctx:
            runner.run(Path("dataset/dev"), {"wrong-id": [{"record_type": "Detection"}]}, "x")
        self.assertIn("image_id", str(ctx.exception))

    def test_coverage_is_reported(self):
        from pathlib import Path
        from eval import eval as runner, loader

        truth = loader.load_set(Path("dataset/dev"))
        only_first = {next(iter(truth)): loader.perfect_predictions(truth)[next(iter(truth))]}
        outcome = runner.run(Path("dataset/dev"), only_first, "x", ci_boot=20)
        cov = outcome["summary"]["coverage"]
        self.assertEqual(cov["prediction_images"], 1)
        self.assertEqual(cov["uncovered_images"], len(truth) - 1)


class FailingImages(unittest.TestCase):
    def test_meta_false_positive_counts(self):
        """元数据误报也算失败，否则"只有元数据多报"的图永远进不了失败清单。"""
        from eval.matcher import ImageResult
        from eval import metrics

        r = ImageResult(image_id="img", meta_false_positives=2)
        self.assertEqual(metrics.failing_images([r]), ["img"])

    def test_clean_image_not_listed(self):
        from eval.matcher import ImageResult
        from eval import metrics

        self.assertEqual(metrics.failing_images([ImageResult(image_id="ok", true_positives=3)]), [])


class OutputHistory(unittest.TestCase):
    def test_same_second_runs_get_distinct_directories(self):
        from eval import eval as runner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "20261102-210000-abc1234"
            first = runner.allocate_output_dir(base)
            second = runner.allocate_output_dir(base)

            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())


class KindsScope(unittest.TestCase):
    """只评指定 kind。

    为什么必须有这个能力：评 A 的检测器时，meta 是 B 的 K-02。全量评会把 24 条 meta 真值
    全算成漏检，召回从 100% 掉到 64.7%——看起来像检测器坏了，实际是评测范围没对齐。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "labels").mkdir()
        truth = [
            {
                "contract_version": "photocheck/v4",
                "record_type": "ImageDescriptor",
                "image_id": "img-1",
                "width": 100,
                "height": 100,
                "orientation": "exif_transposed",
                "coordinate_space": "top_left_pixels",
            },
            det("phone", [10, 10, 50, 20]),
            {
                "contract_version": "photocheck/v4",
                "record_type": "Detection",
                "id": "det-gps",
                "image_id": "img-1",
                "kind": "meta",
                "subtype": "gps",
                "bbox": None,
                "detector_score": None,
                "evidence": "exif",
                "risk": 3,
                "details": {},
            },
        ]
        (root / "labels" / "img-1.json").write_text(
            json.dumps(truth, ensure_ascii=False), encoding="utf-8")
        (root / "negatives.json").write_text("{}", encoding="utf-8")
        self.root = root
        self.preds = {"img-1": [det("phone", [10, 10, 50, 20])]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_kinds_counts_meta_as_miss(self):
        from eval.eval import run

        out = run(self.root, self.preds, "test")
        # meta 真值在，但预测没有 meta：分母含 2，只中 1
        self.assertEqual(out["summary"]["objects_truth"], 2)
        self.assertEqual(out["summary"]["recall"], 0.5)
        self.assertEqual(out["context"]["kinds"], "all")

    def test_text_only_excludes_meta_from_denominator(self):
        from eval.eval import run

        out = run(self.root, self.preds, "test", kinds=("text",))
        self.assertEqual(out["summary"]["objects_truth"], 1)
        self.assertEqual(out["summary"]["recall"], 1.0)
        self.assertEqual(out["context"]["kinds"], ["text"])

    def test_unknown_kind_exits_with_code_2(self):
        from eval.eval import main

        rc = main(["--set", "dev", "--perfect", "--kinds", "phone"])
        self.assertEqual(rc, 2)


class Coverage(unittest.TestCase):
    """覆盖率口径：有预测文件就算覆盖，空数组是干净图的正确结果。"""

    def test_empty_prediction_counts_as_covered(self):
        from eval.eval import run

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "labels").mkdir()
            truth = [
                {
                    "contract_version": "photocheck/v4",
                    "record_type": "ImageDescriptor",
                    "image_id": "img-clean",
                    "width": 100,
                    "height": 100,
                    "orientation": "exif_transposed",
                    "coordinate_space": "top_left_pixels",
                }
            ]
            (root / "labels" / "img-clean.json").write_text(
                json.dumps(truth, ensure_ascii=False), encoding="utf-8")
            (root / "negatives.json").write_text("{}", encoding="utf-8")

            out = run(root, {"img-clean": []}, "test")
            cov = out["summary"]["coverage"]
            self.assertEqual(cov["uncovered_images"], 0)
            self.assertEqual(cov["matched_images"], 1)
            self.assertEqual(cov["foreign_count"], 0)


class MaskAndDuplicateMetrics(unittest.TestCase):
    """掩码正确率与重复检出。

    这两项是 2026-11-07 实拍暴露的：一个号码报成两条、手写号码读错末四位。
    当时召回/精确/误报三项全是满分，**一个都没照出来**。
    """

    T = {"id": "t1", "kind": "text", "subtype": "phone", "bbox": [10, 10, 100, 30],
         "details": {"masked_preview": "138****0000"}}

    def _pred(self, bbox, mask, subtype="phone"):
        return {"kind": "text", "subtype": subtype, "bbox": bbox,
                "details": {"masked_preview": mask}}

    def test_mask_mismatch_is_counted(self):
        """框对、类别对、掩码错 —— 老指标会算成 TP，这里必须另外记一笔。"""
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****9999")])
        self.assertEqual(r.true_positives, 1)
        self.assertEqual(r.mask_checked, 1)
        self.assertEqual(r.mask_mismatches, 1, "掩码不同必须记 mismatch")
        kinds = [f["kind"] for f in r.failures]
        self.assertIn("mask_mismatch", kinds)

    def test_mask_equal_is_clean(self):
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****0000")])
        self.assertEqual((r.mask_checked, r.mask_mismatches), (1, 0))

    def test_mask_skipped_when_either_side_missing(self):
        """真值没给掩码时不该算分母 —— 否则 meta、qr 会把正确率稀释成假数。"""
        pred = {"kind": "text", "subtype": "phone", "bbox": [10, 10, 100, 30], "details": {}}
        r = match_image("img", [self.T], [pred])
        self.assertEqual(r.mask_checked, 0)
        self.assertEqual(r.mask_mismatches, 0)

    def test_duplicate_prediction_is_counted(self):
        """同一号码两个重叠框：1 个 TP + 1 个 FP，但也要单独记 1 条重复。"""
        dup = self._pred([12, 11, 100, 30], "138****0000")
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****0000"), dup])
        self.assertEqual(r.true_positives, 1)
        self.assertEqual(r.false_positives, 1)
        self.assertEqual(r.duplicates, 1, "重叠的同 subtype 预测框要记重复")
        self.assertIn("duplicate", [f["kind"] for f in r.failures])

    def test_disjoint_boxes_are_not_duplicates(self):
        """同 subtype 但不重叠（两个不同的号码）不是重复。"""
        other = self._pred([400, 10, 100, 30], "139****1111")
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****0000"), other])
        self.assertEqual(r.duplicates, 0)

    def test_summary_exposes_both(self):
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****9999")])
        s = metrics.summarize([r])
        self.assertEqual(s["mask_accuracy"], 0.0)
        self.assertEqual(s["mask_checked"], 1)
        self.assertEqual(s["duplicates"], 0)
        self.assertEqual(s["duplicate_rate"], 0.0)

    def test_duplicate_rate_uses_prediction_total(self):
        dup = self._pred([12, 11, 100, 30], "138****0000")
        r = match_image("img", [self.T], [self._pred([10, 10, 100, 30], "138****0000"), dup])
        s = metrics.summarize([r])
        self.assertEqual(s["objects_pred"], 2)
        self.assertAlmostEqual(s["duplicate_rate"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
