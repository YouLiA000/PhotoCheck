"""复检器的真实契约。

**2026-11-08 修正**：这两条用例原先假设 `_verify_region(image, ...)` 接收内存里的
PIL 对象、且不应重开文件。实际 vendored 的 B 版实现签名是 `(output_path, region,
detections, probe)`，**从导出文件重新读**——这是刻意的：复检必须验"落盘的那份"，
而不是内存里那份（否则等于自己验自己）。

所以这里按真实接口重写：给一个临时文件，看它判 passed / failed。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.vendor.photocheck.core.verification import _verify_region


def region(finding_id: str | None = None, method: str = "solid") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"redact:{finding_id or 'manual-001'}",
        finding_id=finding_id,
        method=method,
        applied_bbox=(0, 0, 20, 20),
        source_bbox=(0, 0, 20, 20),
    )


class SolidRegionTests(unittest.TestCase):
    def _verify(self, color: tuple[int, ...], mode: str = "RGB") -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.png"
            Image.new(mode, (20, 20), color).save(path)
            return _verify_region(path, region(), [], lambda *a, **k: {})

    def test_black_manual_region_is_structurally_valid_but_pending(self) -> None:
        check = self._verify((0, 0, 0))
        self.assertEqual(check["status"], "not_applicable")
        self.assertEqual(check["method"], "solid_black_region_structural_only")

    def test_white_region_fails(self) -> None:
        """纯色块没盖严（还是白的）必须判失败——否则报告会骗人。"""
        self.assertEqual(self._verify((255, 255, 255))["status"], "failed")

    def test_opaque_black_rgba_is_pending(self) -> None:
        check = self._verify((0, 0, 0, 255), mode="RGBA")
        self.assertEqual(check["status"], "not_applicable")

    def test_missing_file_does_not_raise(self) -> None:
        """文件读不出来时判失败，而不是抛异常把整份报告带崩。"""
        check = _verify_region(Path("no-such-file.png"), region(), [], lambda *a, **k: {})
        self.assertEqual(check["status"], "failed")


class FindingRegionTests(unittest.TestCase):
    def _run(self, kind: str, evidence: dict, finding_id: str = "text-1") -> dict:
        seen: list[object] = []

        def probe(path: object, bbox: object, *, kinds: tuple[str, ...]) -> dict:
            seen.append((path, bbox, kinds))
            return evidence

        detection = {"id": finding_id, "kind": kind}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.png"
            Image.new("RGB", (20, 20), "white").save(path)
            check = _verify_region(path, region(finding_id), [detection], probe)
        self.assertTrue(seen, "内容探针必须被调用")
        self.assertEqual(seen[0][2], (kind,), "探针只查这一类的残留")
        return check

    def test_text_without_residual_passes(self) -> None:
        self.assertEqual(self._run("text", {"residual_text": 0})["status"], "passed")

    def test_text_with_residual_fails(self) -> None:
        self.assertEqual(self._run("text", {"residual_text": 3})["status"], "failed")

    def test_qr_with_residual_detected_fails(self) -> None:
        check = self._run("qr", {"residual_qr_detected": 1, "residual_qr_decodable": 0})
        self.assertEqual(check["status"], "failed")

    def test_qr_clean_passes(self) -> None:
        check = self._run("qr", {"residual_qr_detected": 0, "residual_qr_decodable": 0})
        self.assertEqual(check["status"], "passed")

    def test_unavailable_detector_is_not_applicable(self) -> None:
        """检测器不可用 ≠ 通过，必须是 not_applicable。"""
        check = self._run("text", {"unavailable": ("text",), "residual_text": 0})
        self.assertEqual(check["status"], "not_applicable")

    def test_probe_error_fails(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("engine down")

        detection = {"id": "text-1", "kind": "text"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.png"
            Image.new("RGB", (20, 20), "white").save(path)
            check = _verify_region(path, region("text-1"), [detection], boom)
        self.assertEqual(check["status"], "failed")


if __name__ == "__main__":
    unittest.main()
