from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.integrate_r03_results import _layer2, _layer3


class R03IntegrationTests(unittest.TestCase):
    def test_layer2_requires_every_export_to_be_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer2.json"
            path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {"residual_count": 0, "metadata_tags": [], "disposal_state": "passed"},
                            {"residual_count": 1, "metadata_tags": [], "disposal_state": "failed"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            layer = _layer2(path)
        self.assertEqual(layer["status"], "failed")
        self.assertEqual(layer["clean"], 1)

    def test_layer3_scope_is_carried_into_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer3.json"
            path.write_text(
                json.dumps(
                    {
                        "reviewed_images": 38,
                        "appropriate": 38,
                        "over_redacted": 0,
                        "under_redacted": 0,
                        "reviewers": 2,
                        "agreed": 38,
                        "scope": "five classes",
                    }
                ),
                encoding="utf-8",
            )
            layer = _layer3(path)
        self.assertEqual(layer["status"], "passed")
        self.assertEqual(layer["scope"], "five classes")


if __name__ == "__main__":
    unittest.main()
