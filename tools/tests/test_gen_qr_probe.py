"""二维码探针生成器的门禁测试。

只锁三件事：源图必须能解出、退化图不能改画布尺寸、manifest 不得泄露 payload。
第三件是隐私红线——payload 一旦进了 CSV，就已经违规了。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gen_qr_probe as g  # noqa: E402


class SourceGate(unittest.TestCase):
    def test_every_source_code_is_decodable(self):
        for pid, _subtype, payload in g.PAYLOADS:
            with self.subTest(payload_id=pid):
                code = g.build_qr(payload)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "probe.png"
                    code.save(path)
                    self.assertEqual(
                        g.decode(path), payload, f"{pid} 的源图解不出或内容不一致"
                    )

    def test_conditions_keep_canvas_size(self):
        code = g.build_qr("PHOTOCHECK-PROBE-2026-11-16")
        for cond, _note in g.CONDITIONS:
            with self.subTest(condition=cond):
                self.assertEqual(g.degrade(code, cond).size, g.CANVAS)

    def test_unknown_condition_rejected(self):
        code = g.build_qr("x")
        with self.assertRaises(ValueError):
            g.degrade(code, "not-a-condition")


class ManifestPrivacy(unittest.TestCase):
    def test_manifest_has_no_payload_column_and_sources_are_decodable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe"
            with mock.patch.object(sys, "argv", ["gen_qr_probe.py", "--out", str(out)]):
                self.assertEqual(g.main(), 0)

            with (out / "manifest.csv").open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), len(g.PAYLOADS) * (1 + len(g.CONDITIONS)))
            header = " ".join(rows[0].keys()).lower()
            self.assertNotIn("payload", header.replace("payload_id", ""))
            self.assertNotIn("content", header)

            sources = [r for r in rows if r["group"] == "source"]
            self.assertEqual(len(sources), len(g.PAYLOADS))
            self.assertTrue(all(r["decodable"] == "yes" for r in sources))

            # 不得把二维码原文写进任何一列
            blob = (out / "manifest.csv").read_text(encoding="utf-8")
            for _pid, _subtype, payload in g.PAYLOADS:
                self.assertNotIn(payload, blob)


class ForceGuard(unittest.TestCase):
    def test_force_is_confined_to_dataset_probe_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                g.validate_force_target(Path(tmp) / "probe_qr_external")

    def test_force_accepts_the_expected_dataset_directory(self):
        expected = g.ROOT / "dataset" / "probe_qr_test"
        self.assertEqual(g.validate_force_target(expected), expected.resolve())


class PrintRaster(unittest.TestCase):
    """打印页的高 dpi 栅格化必须仍然可解——否则打出来就是废纸。"""

    def test_high_dpi_render_is_decodable(self):
        from tools import make_qr_printsheet as ps

        for pid, _subtype, payload in g.PAYLOADS:
            with self.subTest(payload_id=pid):
                img = ps.render_qr(payload, 600)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "print.png"
                    img.save(path)
                    self.assertEqual(g.decode(path), payload)

    def test_render_is_square(self):
        from tools import make_qr_printsheet as ps

        img = ps.render_qr("PHOTOCHECK-PROBE-2026-11-16", 600)
        self.assertEqual(img.width, img.height)


if __name__ == "__main__":
    unittest.main(verbosity=2)
