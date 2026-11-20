"""运维脚本的边界测试。

这些脚本是提交前/交付前的最后一道门，**失败时不能放行**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract.masking import mask_idcard, mask_phone  # noqa: E402
from tools import build_app, privacy_scan, smoke  # noqa: E402


class PrivacyScan(unittest.TestCase):
    def test_masking_uses_contract_single_source(self):
        hits = privacy_scan._scan_text(
            "联系电话 13800138000，证件 110101199003071234"
        )
        self.assertEqual(hits, [
            (1, "phone", mask_phone("13800138000")),
            (1, "idcard", mask_idcard("110101199003071234")),
        ])

    def test_real_dataset_is_not_blanket_whitelisted(self):
        self.assertFalse(privacy_scan._is_allowed("dataset/real/labels.csv"))
        self.assertTrue(privacy_scan._is_allowed("dataset/dev/labels.csv"))
        self.assertFalse(privacy_scan._is_allowed(
            "photocheck/detectors/a_engine/reports/example.md"
        ))

    def test_missing_explicit_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp, "missing.json")
            self.assertEqual(privacy_scan.main([str(missing)]), 2)

    def test_unreadable_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.json")
            target.write_text("{}", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=OSError("拒绝访问")):
                code = privacy_scan.main([str(target)])
        self.assertEqual(code, 2)

    def test_git_failure_is_not_treated_as_empty_stage(self):
        failed = Mock(returncode=1, stdout=b"", stderr=b"fatal")
        with patch.object(privacy_scan.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError):
                privacy_scan._staged_files()


class SmokeAudit(unittest.TestCase):
    @staticmethod
    def _payload():
        return json.dumps({"checks": [
            {"id": "CT-01", "title": "ok", "owner": "C", "status": "PASS"}
        ]})

    def test_malformed_audit_output_does_not_pass(self):
        with patch.object(smoke, "_run", return_value=(0, "not json")):
            ok, _ = smoke._audit_c_only()
        self.assertFalse(ok)

    def test_audit_internal_error_does_not_pass(self):
        with patch.object(smoke, "_run", return_value=(2, self._payload())):
            ok, _ = smoke._audit_c_only()
        self.assertFalse(ok)

    def test_expected_a_b_failure_still_passes_c_gate(self):
        with patch.object(smoke, "_run", return_value=(1, self._payload())):
            ok, detail = smoke._audit_c_only()
        self.assertTrue(ok)
        self.assertIn("1/1", detail)

    def test_explicit_skip_does_not_fail_c_gate(self):
        payload = json.dumps({"checks": [
            {"id": "CT-01", "title": "ok", "owner": "C", "status": "PASS"},
            {"id": "EN-05", "title": "optional", "owner": "C", "status": "SKIP"},
        ]})
        with patch.object(smoke, "_run", return_value=(1, payload)):
            ok, detail = smoke._audit_c_only()
        self.assertTrue(ok)
        self.assertIn("1 项明确跳过", detail)


class PortableBundleAudit(unittest.TestCase):
    def test_portable_launchers_are_ascii_and_self_diagnosing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_app.write_launchers(root)
            for name in ("启动.bat", "Start.bat", "自检.bat", "Check.bat", "README_FIRST.txt"):
                self.assertTrue((root / name).is_file(), name)
            launcher = (root / "Start.bat").read_text(encoding="utf-8")
            self.assertIn("runtime\\python.exe", launcher)
            self.assertIn("runtime is incomplete", launcher)
            self.assertIn("PYTHONNOUSERSITE", launcher)
            first = (root / "README_FIRST.txt").read_text(encoding="ascii")
            self.assertIn("Extract the ENTIRE ZIP", first)

    def test_runtime_debris_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "work").mkdir()
            (root / "work" / "upload.jpg").write_bytes(b"x")
            (root / "module.pyc").write_bytes(b"x")
            debris = set(build_app._runtime_debris(root))
            self.assertIn("work", debris)
            self.assertIn("work/upload.jpg", debris)
            self.assertIn("module.pyc", debris)

    def test_missing_required_contract_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(
                "docs/功能规格卡_v1.4.md",
                build_app._missing_required(Path(tmp)),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
