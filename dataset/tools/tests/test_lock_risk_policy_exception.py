"""Lock labels are historical bytes; runtime risk follows the current policy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.risk_rules import detection_risk  # noqa: E402


class LockRiskPolicyException(unittest.TestCase):
    def test_sealed_risk_two_is_recomputed_as_three_at_runtime(self) -> None:
        for image_id in ("image-lock-003", "image-lock-015"):
            with self.subTest(image_id=image_id):
                path = ROOT / "dataset" / "lock" / "labels" / f"{image_id}.json"
                records = json.loads(path.read_text(encoding="utf-8"))
                qr = next(
                    record for record in records
                    if record.get("record_type") == "Detection"
                    and record.get("subtype") == "qr_wechat_pay"
                )
                self.assertEqual(qr["risk"], 2, "封存标签不能被重写")
                self.assertEqual(
                    detection_risk(qr["kind"], qr["subtype"], qr.get("details")),
                    3,
                    "运行时必须按 S-3 当前规则重新计算",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
