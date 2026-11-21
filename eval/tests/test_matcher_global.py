"""匹配必须是全局最优，不能是「IoU 降序贪心」。

贪心是局部最优：真值 A 对预测 1/2 的 IoU 为 0.9/0.8，真值 B 对 1/2 为 0.8/不可行时，
贪心先吃 (A,1)，B 落空 → 1 对；最优解 (A,2)+(B,1) → 2 对。
框密集或重叠时，这会系统性抬高 FN 与 FP，而「真值当预测」自测恒为 1.0，测不出来。

本文件用两层锁：算法层与暴力枚举对照，集成层验证一对一不被破坏。
"""

from __future__ import annotations

import itertools
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.matcher import match_image, max_weight_matching  # noqa: E402


def brute_force(scores):
    """小规模穷举：返回 (最大配对数, 该配对数下的最大权和)。"""
    n = len(scores)
    m = len(scores[0]) if n else 0
    best = (0, 0.0)
    cols = range(m)
    for k in range(min(n, m), -1, -1):
        found = None
        for rows in itertools.combinations(range(n), k):
            for perm in itertools.permutations(cols, k):
                total = 0.0
                ok = True
                for i, j in zip(rows, perm):
                    s = scores[i][j]
                    if s is None:
                        ok = False
                        break
                    total += s
                if ok and (found is None or total > found):
                    found = total
        if found is not None:
            best = (k, found)
            break
    return best


class GlobalOptimum(unittest.TestCase):
    def test_textbook_counterexample(self):
        # 报告里的反例：贪心 1 对，最优 2 对
        scores = [[0.9, 0.8], [0.8, None]]
        pairs = max_weight_matching(scores)
        self.assertEqual(len(pairs), 2, pairs)
        total = sum(scores[i][j] for i, j in pairs)
        self.assertAlmostEqual(total, 1.6, places=6)

    def test_prefers_more_pairs_over_one_high_iou(self):
        # 一对 0.95 与两对各 0.6：应先保证配对数
        scores = [[0.95, 0.6], [None, 0.6]]
        pairs = max_weight_matching(scores)
        self.assertEqual(len(pairs), 2, pairs)

    def test_respects_forbidden_edges(self):
        scores = [[None, 0.7], [0.7, None]]
        self.assertEqual(sorted(max_weight_matching(scores)), [(0, 1), (1, 0)])

    def test_random_matrices_match_brute_force(self):
        rng = random.Random(20261120)
        for _ in range(60):
            n, m = rng.randint(0, 4), rng.randint(0, 4)
            if not n or not m:
                continue
            scores = [
                [None if rng.random() < 0.35 else round(rng.uniform(0.5, 1.0), 3) for _ in range(m)]
                for _ in range(n)
            ]
            pairs = max_weight_matching(scores)
            got_k = len(pairs)
            got_sum = sum(scores[i][j] for i, j in pairs)
            want_k, want_sum = brute_force(scores)
            self.assertEqual(got_k, want_k, f"配对数不对：{scores} -> {pairs}")
            self.assertAlmostEqual(got_sum, want_sum, places=6, msg=f"总权不对：{scores}")

    def test_empty_and_all_forbidden(self):
        self.assertEqual(max_weight_matching([]), [])
        self.assertEqual(max_weight_matching([[], []]), [])
        self.assertEqual(max_weight_matching([[None, None], [None, None]]), [])


class OneToOne(unittest.TestCase):
    def test_no_box_is_used_twice(self):
        truth = [
            {"id": "t1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 40]},
            {"id": "t2", "kind": "text", "subtype": "phone", "bbox": [0, 30, 100, 40]},
        ]
        pred = [
            {"id": "p1", "kind": "text", "subtype": "phone", "bbox": [0, 0, 100, 40]},
            {"id": "p2", "kind": "text", "subtype": "phone", "bbox": [0, 28, 100, 40]},
            {"id": "p3", "kind": "text", "subtype": "phone", "bbox": [0, 31, 100, 40]},
        ]
        res = match_image("img", truth, pred)
        self.assertLessEqual(res.tp_total + res.subtype_errors, len(truth))
        self.assertEqual(res.misses + res.true_positives + res.subtype_errors, len(truth))
        self.assertEqual(res.false_positives + res.true_positives + res.subtype_errors, len(pred))


if __name__ == "__main__":
    unittest.main(verbosity=2)
