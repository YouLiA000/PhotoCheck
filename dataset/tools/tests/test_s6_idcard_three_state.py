"""S-6 身份证三态的数据侧回归。

规则（2026-11-15 共识）：

| 校验位 | 生日段 | 标注 |
|---|---|---|
| pass | 合法 | 正例 pass / risk 3 |
| **pass** | **非法** | **负例**（构造流水号） |
| **fail** | **任意** | **正例 fail / risk 2** |

这两条基准样本必须一直保持相反的方向，否则说明有人把门禁改回了
"生日非法一律不报"——那会让 lock-014 变成漏检。
"""

from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DEV = ROOT / "dataset" / "dev"
LOCK = ROOT / "dataset" / "lock"


class S6IdCardThreeState(unittest.TestCase):
    def labels(self, base: Path, image_id: str) -> list[dict]:
        path = base / "labels" / f"{image_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def negatives(self, base: Path, image_id: str) -> list[dict]:
        path = base / "negatives.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(image_id) or []

    def csv_rows(self, base: Path) -> list[dict]:
        with (base / "labels.csv").open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_dev_031_pass_plus_invalid_is_negative(self):
        """校验位通过 + 生日非法 → 不生成 idcard，登记为负例。"""
        recs = self.labels(DEV, "image-dev-031")
        idcards = [
            r for r in recs
            if r.get("record_type") == "Detection" and r.get("subtype") == "idcard"
        ]
        self.assertEqual(idcards, [], "dev-031 是构造流水号，不该有 idcard Detection")

        neg = self.negatives(DEV, "image-dev-031")
        self.assertTrue(
            any(x.get("bbox") == [303, 441, 366, 32] for x in neg),
            "dev-031 的身份证负例区域丢了",
        )
        self.assertTrue(
            any("生日" in (x.get("note") or "") for x in neg),
            "负例 note 必须写清依赖生日门禁",
        )

    def test_lock_014_fail_plus_invalid_is_positive(self):
        """校验位失败 + 生日非法 → 仍报 fail / risk 2，不是负例。"""
        recs = self.labels(LOCK, "image-lock-014")
        idcards = [
            r for r in recs
            if r.get("record_type") == "Detection" and r.get("subtype") == "idcard"
        ]
        self.assertEqual(len(idcards), 1, "lock-014 应有一条 idcard 正例")
        rec = idcards[0]
        self.assertEqual(rec["details"].get("idcard_check"), "fail")
        self.assertEqual(rec["risk"], 2)

        neg = self.negatives(LOCK, "image-lock-014")
        self.assertFalse(
            any(x.get("bbox") == [247, 500, 294, 27] for x in neg),
            "fail + 生日非法不是负例；把它放回 negatives 会让正例变漏检",
        )

    def test_csv_verdicts_agree_with_labels(self):
        """CSV 与 JSON 必须同向：dev-031 负、lock-014 正。"""
        dev = self.csv_rows(DEV)
        self.assertTrue(
            any(r["file"].endswith("image-dev-031.jpg")
                and r["verdict"] == "negative" for r in dev),
            "dev-031 在 CSV 里应是 negative",
        )

        lock = self.csv_rows(LOCK)
        self.assertTrue(
            any(r["file"].endswith("image-lock-014.jpg")
                and r["verdict"] == "positive"
                and r["subtype"] == "idcard"
                and r["idcard_check"] == "fail" for r in lock),
            "lock-014 在 CSV 里应是 positive + idcard + fail",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
