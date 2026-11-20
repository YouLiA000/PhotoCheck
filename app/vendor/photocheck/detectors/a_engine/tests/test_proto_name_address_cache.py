"""The name/address probe must fail clearly when its local cache is absent."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from photocheck.detectors.a_engine.scripts import proto_name_address as probe


class CacheResolution(unittest.TestCase):
    def test_missing_default_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(probe.resolve_ocr_cache(None, Path(tmp)))

    def test_explicit_cache_takes_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit.json"
            explicit.write_text("{}", encoding="utf-8")
            self.assertEqual(probe.resolve_ocr_cache(str(explicit), Path(tmp)), explicit)

    def test_existing_default_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp)
            default = here / "reports" / "dev_ocr_cache.json"
            default.parent.mkdir()
            default.write_text("{}", encoding="utf-8")
            self.assertEqual(probe.resolve_ocr_cache(None, here), default)

    def test_explicit_missing_path_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(probe.resolve_ocr_cache(
                str(Path(tmp) / "missing.json"), Path(tmp)
            ))

    def test_missing_cache_returns_actionable_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = probe.main(["--ocr-cache", str(Path(tmp) / "missing.json")])
            self.assertEqual(code, 2)
            self.assertIn("--images-dir", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_csv_does_not_persist_candidate_text(self):
        blocks = {
            "image-1": [{
                "bbox": [10, 20, 120, 30],
                "score": 0.99,
                "text": "张三 13800138000 user@example.com",
            }]
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.json"
            cache.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
            out = root / "out"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = probe.main([
                    "--ocr-cache", str(cache),
                    "--out", str(out),
                ])
            self.assertEqual(code, 0)
            text = (out / "candidates.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("张三", text)
            self.assertNotIn("13800138000", text)
            self.assertNotIn("user@example.com", text)
            with (out / "candidates.csv").open(encoding="utf-8-sig", newline="") as fh:
                header = next(csv.reader(fh))
            self.assertNotIn("text", header)
            self.assertIn("text_sha256", header)


if __name__ == "__main__":
    unittest.main(verbosity=2)
