"""契约 v4 校验器测试。只依赖标准库。

    python -m unittest discover -s contract/tests -v
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract import migrate_v3  # noqa: E402
from contract.validate_records import (  # noqa: E402
    _SchemaChecker,
    _derive_disposal_state,
    assert_supported,
    load_records,
    load_schema,
    validate_records,
)

VALID = ROOT / "contract" / "examples" / "valid_pipeline.json"
B_VALID = ROOT / "photocheck" / "contract" / "examples" / "valid_records.json"


def get(records, record_type, ident=None):
    for rec in records:
        if rec["record_type"] != record_type:
            continue
        if ident is None:
            return rec
        if ident in (
            rec.get("id"),
            rec.get("finding_id"),
            rec.get("image_id"),
            rec.get("region_id"),
        ):
            return rec
    raise KeyError(f"{record_type} / {ident}")


def codes(report):
    return {issue.code for issue in report.errors}


def paths(report):
    return " | ".join(issue.path for issue in report.errors)


class SchemaSelfCheck(unittest.TestCase):
    def test_schema_uses_only_supported_keywords(self):
        assert_supported(load_schema())

    def test_every_ref_resolves(self):
        schema = load_schema()
        checker = _SchemaChecker(schema)
        found = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref":
                        found.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
        self.assertTrue(found, "schema 里应当有 $ref")
        for ref in found:
            self.assertIsInstance(checker._resolve(ref), dict, ref)

    def test_examples_are_json_readable(self):
        self.assertTrue(json.loads(VALID.read_text(encoding="utf-8")))

    def test_runtime_example_passes_shared_validator(self):
        records = json.loads(B_VALID.read_text(encoding="utf-8"))
        report = validate_records(records)
        self.assertTrue(report.ok, report.to_dict())

    def test_runtime_and_contract_schema_are_byte_identical(self):
        contract_schema = ROOT / "contract" / "schema" / "photocheck-v4.schema.json"
        runtime_schema = ROOT / "photocheck" / "contract" / "schema" / "photocheck-v4.schema.json"
        self.assertEqual(
            hashlib.sha256(contract_schema.read_bytes()).hexdigest(),
            hashlib.sha256(runtime_schema.read_bytes()).hexdigest(),
        )


class StructLayer(unittest.TestCase):
    def setUp(self):
        self.records = load_records(VALID)

    def assert_error_at(self, records, fragment):
        report = validate_records(records)
        self.assertFalse(report.ok, f"应当报错（期望命中 {fragment}），实际全部通过")
        self.assertIn(fragment, paths(report), f"错误位置里没有 {fragment}：{paths(report)}")
        return report

    def test_valid_pipeline_passes(self):
        report = validate_records(self.records)
        self.assertTrue(report.ok, [str(i) for i in report.errors])
        self.assertEqual(report.record_count, 10)

    def test_unknown_field_rejected(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Decision").update({"selected": True})
        self.assert_error_at(recs, "selected")

    def test_contract_version_is_fixed(self):
        recs = copy.deepcopy(self.records)
        get(recs, "ImageDescriptor")["contract_version"] = "photocheck/v3"
        self.assert_error_at(recs, "contract_version")

    def test_version_constant_is_consistent_across_modules(self):
        """版本号散落在 6 处，改一处漏一处会产出"版本不符"的记录。

        不强行收敛成单一常量：`validate_records.py` 要能直接当脚本跑
        （`python contract/validate_records.py x.json`），改成包内相对导入会把它弄坏。
        改用测试锁：任何一处漏改，这里立刻红，而不是等到产出记录被校验拒绝。
        """
        expected = 'CONTRACT_VERSION = "photocheck/v4"'
        carriers = [
            "contract/migrate_v3.py",
            "contract/risk_rules.py",
            "contract/validate_records.py",
            "dataset/tools/gen_samples.py",
            "dataset/tools/labels_csv.py",
            "ui/decision.py",
        ]
        for rel in carriers:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(expected, text, f"{rel} 的 CONTRACT_VERSION 没跟上升版")

    def test_detector_score_range(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["detector_score"] = 1.5
        self.assert_error_at(recs, "detector_score")

    def test_meta_bbox_must_be_null(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-meta-gps")["bbox"] = [0, 0, 10, 10]
        self.assert_error_at(recs, "bbox")

    def test_text_bbox_required(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["bbox"] = None
        self.assert_error_at(recs, "bbox")

    def test_evidence_follows_kind(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["evidence"] = "exif"
        self.assert_error_at(recs, "evidence")

    def test_subtype_follows_kind(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["subtype"] = "qr_wifi"
        self.assert_error_at(recs, "subtype")

    def test_idcard_requires_check_tristate(self):
        recs = copy.deepcopy(self.records)
        del get(recs, "Detection", "det-demo-id-01")["details"]["idcard_check"]
        self.assert_error_at(recs, "idcard_check")

    def test_masked_preview_must_be_masked(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["details"]["masked_preview"] = "13800138000"
        self.assert_error_at(recs, "masked_preview")

    def test_face_details_must_be_empty(self):
        recs = copy.deepcopy(self.records)
        face = {
            "contract_version": "photocheck/v4",
            "record_type": "Detection",
            "id": "det-demo-face-01",
            "image_id": "image-demo-001",
            "kind": "face",
            "subtype": "face_region",
            "bbox": [10, 10, 40, 40],
            "detector_score": 0.9,
            "evidence": "face_box",
            "risk": 1,
            "details": {"age": 30},
        }
        recs.append(face)
        self.assert_error_at(recs, "age")

    def test_builtin_and_jsonschema_agree_if_available(self):
        try:
            import jsonschema  # type: ignore  # noqa: F401
        except ImportError:
            self.skipTest("未安装 jsonschema，只用内置校验器")
        schema = load_schema()
        for rec in self.records:
            builtin = _SchemaChecker(schema).validate(rec)
            self.assertEqual(builtin, [], f"内置校验器与合法样例冲突：{builtin}")


class LeakRedline(unittest.TestCase):
    def setUp(self):
        self.records = load_records(VALID)

    def test_raw_exif_key_rejected(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-meta-gps")["details"]["raw_exif"] = {
            "GPSLatitude": "40.0 N"
        }
        report = validate_records(recs)
        self.assertFalse(report.ok)
        self.assertIn("forbidden_key", codes(report))

    def test_payload_ref_rejected(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-qr-01")["details"]["masked_summary"] = "WIFI:S:home"
        get(recs, "Detection", "det-demo-qr-01")["payload_ref"] = "mem://qr-1"
        report = validate_records(recs)
        self.assertIn("forbidden_key", codes(report))

    def test_raw_value_field_rejected_even_if_null(self):
        # 规格卡 §0.2：原文不进任何可序列化记录。留一个恒为 null 的 raw_value 也算开口子
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["details"]["raw_value"] = None
        report = validate_records(recs)
        self.assertIn("forbidden_key", codes(report))

    def test_unmasked_phone_in_preview(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["details"]["masked_preview"] = "138****0000 13800138000"
        report = validate_records(recs)
        self.assertIn("unmasked_preview", codes(report))


class CrossRecord(unittest.TestCase):
    def setUp(self):
        self.records = load_records(VALID)

    def assert_code(self, records, code):
        report = validate_records(records)
        self.assertFalse(report.ok, f"应当报 {code}，实际全部通过")
        self.assertIn(code, codes(report), f"实际错误：{sorted(codes(report))}")
        return report

    def test_duplicate_detection_id(self):
        recs = copy.deepcopy(self.records)
        recs.append(copy.deepcopy(get(recs, "Detection", "det-demo-phone-01")))
        self.assert_code(recs, "duplicate_detection_id")

    def test_ambiguous_finding_id_across_images(self):
        # 规格卡 0.2 只要求 id 在单个 image_id 内唯一；跨图重名只让 Decision 失去消歧能力，
        # 属于提示而非违约。用最小批量单独验证，不牵动整条闭环，免得引入别的错误。
        def desc(image_id):
            return {
                "contract_version": "photocheck/v4", "record_type": "ImageDescriptor",
                "image_id": image_id, "width": 800, "height": 600,
                "orientation": "as_stored", "coordinate_space": "top_left_pixels",
            }

        def det(image_id):
            return {
                "contract_version": "photocheck/v4", "record_type": "Detection",
                "id": "det-shared", "image_id": image_id, "kind": "text", "subtype": "phone",
                "bbox": [10, 10, 200, 40], "detector_score": 0.9, "evidence": "ocr_rule",
                "risk": 2, "details": {"masked_preview": "138****0000"},
            }

        recs = [desc("image-a"), desc("image-b"), det("image-a"), det("image-b")]
        report = validate_records(recs)
        self.assertTrue(report.ok, [str(i) for i in report.errors])
        self.assertIn("ambiguous_finding_id", {i.code for i in report.warnings})

    def test_bbox_out_of_bounds(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Detection", "det-demo-phone-01")["bbox"] = [1500, 410, 220, 42]
        self.assert_code(recs, "bbox_out_of_bounds")

    def test_decision_on_metadata(self):
        recs = copy.deepcopy(self.records)
        recs.append(
            {
                "contract_version": "photocheck/v4",
                "record_type": "Decision",
                "finding_id": "det-demo-meta-gps",
                "action": "redact",
                "revision": 1,
            }
        )
        self.assert_code(recs, "decision_on_metadata")

    def test_decision_orphan(self):
        recs = copy.deepcopy(self.records)
        recs.append(
            {
                "contract_version": "photocheck/v4",
                "record_type": "Decision",
                "finding_id": "det-not-exist",
                "action": "redact",
                "revision": 1,
            }
        )
        self.assert_code(recs, "decision_orphan")

    def test_qr_must_be_solid(self):
        recs = copy.deepcopy(self.records)
        get(recs, "RedactionPlan")["items"][1]["method"] = "blur"
        self.assert_code(recs, "qr_method_not_solid")

    def test_redact_without_plan_item(self):
        recs = copy.deepcopy(self.records)
        recs.append(
            {
                "contract_version": "photocheck/v4",
                "record_type": "Detection",
                "id": "det-demo-face-01",
                "image_id": "image-demo-001",
                "kind": "face",
                "subtype": "face_region",
                "bbox": [10, 10, 40, 40],
                "detector_score": 0.9,
                "evidence": "face_box",
                "risk": 1,
                "details": {},
            }
        )
        recs.append(
            {
                "contract_version": "photocheck/v4",
                "record_type": "Decision",
                "finding_id": "det-demo-face-01",
                "action": "redact",
                "revision": 1,
            }
        )
        self.assert_code(recs, "redact_without_plan_item")

    def test_plan_item_without_redact_decision(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Decision", "det-demo-phone-01")["action"] = "retain"
        self.assert_code(recs, "plan_item_without_redact_decision")

    def test_stale_verification(self):
        recs = copy.deepcopy(self.records)
        get(recs, "VerificationReport")["plan_revision"] = 1
        self.assert_code(recs, "stale_verification")

    def test_report_without_plan(self):
        recs = [r for r in copy.deepcopy(self.records) if r["record_type"] != "RedactionPlan"]
        self.assert_code(recs, "report_without_plan")

    def test_disposal_state_must_be_derived(self):
        recs = copy.deepcopy(self.records)
        get(recs, "VerificationReport")["checks"][0]["status"] = "not_run"
        self.assert_code(recs, "disposal_state_mismatch")

    def test_failed_check_needs_residual(self):
        recs = copy.deepcopy(self.records)
        report = get(recs, "VerificationReport")
        report["checks"][0]["status"] = "failed"
        report["disposal_state"] = "failed"
        self.assert_code(recs, "failed_check_without_residual")

    def test_residual_retained_needs_retain_decision(self):
        recs = copy.deepcopy(self.records)
        get(recs, "VerificationReport")["residuals"][0]["finding_id"] = "det-demo-phone-01"
        self.assert_code(recs, "residual_retained_orphan")

    def test_all_retained_is_not_applicable(self):
        recs = copy.deepcopy(self.records)
        plan = get(recs, "RedactionPlan")
        plan["items"] = []
        plan["revision"] = 3
        for fid in ("det-demo-phone-01", "det-demo-qr-01"):
            get(recs, "Decision", fid)["action"] = "retain"
        # 全局版次：计划升到 3，所有当前决定必须一起升到 3（B 的跨记录不变量）
        for rec in recs:
            if rec["record_type"] == "Decision":
                rec["revision"] = 3
        rep = get(recs, "VerificationReport")
        rep["plan_revision"] = 3
        rep["checks"] = []
        rep["disposal_state"] = "not_applicable"
        rep["residuals"] = [
            {
                "region_id": None,
                "finding_id": fid,
                "reason": "retained",
                "kind": "text" if fid == "det-demo-phone-01" else "qr",
                "subtype": "phone" if fid == "det-demo-phone-01" else "qr_contact",
                "masked_summary": "text/phone" if fid == "det-demo-phone-01" else "qr/qr_contact",
            }
            for fid in ("det-demo-id-01", "det-demo-phone-01", "det-demo-qr-01")
        ]
        report = validate_records(recs)
        self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_multiple_plans_only_latest_counts(self):
        recs = copy.deepcopy(self.records)
        old = copy.deepcopy(get(recs, "RedactionPlan"))
        old["revision"] = 1
        old["items"] = []
        recs.append(old)
        report = validate_records(recs)
        self.assertTrue(report.ok, [str(i) for i in report.errors])
        self.assertIn("multiple_plans", {i.code for i in report.warnings})

    def test_every_plan_item_requires_exactly_one_check(self):
        recs = copy.deepcopy(self.records)
        get(recs, "VerificationReport")["checks"].pop()
        report = validate_records(recs)
        self.assertIn("missing_verification_check", codes(report))

    def test_not_run_is_failed_and_requires_a_residual(self):
        recs = copy.deepcopy(self.records)
        report = get(recs, "VerificationReport")
        report["checks"][0]["status"] = "not_run"
        report["disposal_state"] = "failed"
        region_id = report["checks"][0]["region_id"]
        report["residuals"].append(
            {
                "region_id": region_id,
                "finding_id": None,
                "reason": "verification_failed",
                "kind": "manual",
                "subtype": "region",
                "masked_summary": "manual/region",
            }
        )
        validation = validate_records(recs)
        self.assertTrue(validation.ok, [str(i) for i in validation.errors])


class Migration(unittest.TestCase):
    def test_hide_maps_to_redact(self):
        out = migrate_v3.migrate_decision(
            {"id": "det-1", "selected": True, "action": "hide", "verify_status": "ok"}
        )
        self.assertEqual(out["action"], "redact")
        self.assertNotIn("verify_status", out)
        self.assertEqual(out["contract_version"], "photocheck/v4")

    def test_keep_and_skip_map_to_retain(self):
        for action in ("keep", "skip"):
            out = migrate_v3.migrate_decision(
                {"id": "det-1", "selected": False, "action": action}
            )
            self.assertEqual(out["action"], "retain")

    def test_ambiguous_combination_refused(self):
        for selected, action in ((True, "keep"), (False, "hide"), (True, "skip")):
            with self.assertRaises(migrate_v3.MigrationRefused):
                migrate_v3.migrate_decision(
                    {"id": "det-1", "selected": selected, "action": action}
                )

    def test_plaintext_refs_refused(self):
        for key in ("text_ref", "payload_ref", "raw_exif"):
            with self.assertRaises(migrate_v3.MigrationRefused):
                migrate_v3.migrate_decision(
                    {"id": "det-1", "selected": True, "action": "hide", key: "x"}
                )

    def test_batch_returns_refusals(self):
        migrated, refused = migrate_v3.migrate_records(
            [
                {"id": "a", "selected": True, "action": "hide"},
                {"id": "b", "selected": True, "action": "keep"},
                {"id": "c", "selected": False, "action": "skip"},
            ]
        )
        self.assertEqual([m["finding_id"] for m in migrated], ["a", "c"])
        self.assertEqual([r["id"] for r in refused], ["b"])

    def test_migrated_records_pass_validation(self):
        migrated, _ = migrate_v3.migrate_records(
            # v3 没有全局版次；迁进某个快照时必须显式声明它属于哪一版，
            # 否则会得到 revision=1，与快照的 2 混装（B 的校验器会拒绝）。
            [{"id": "det-demo-phone-01", "selected": True, "action": "hide", "revision": 2}]
        )
        records = json.loads(VALID.read_text(encoding="utf-8"))
        records = [
            r
            for r in records
            if not (
                r["record_type"] == "Decision" and r["finding_id"] == "det-demo-phone-01"
            )
        ] + migrated
        report = validate_records(records)
        self.assertTrue(report.ok, [str(i) for i in report.errors])



class RevisionInvariant(unittest.TestCase):
    """全局版次：同一张图的当前决定 + 计划必须共享一个 revision。

    这是 B 在 2026-11-14 指出的跨记录不变量。C 的 DecisionBuilder 本来就是全局版次，
    但校验器一直没查——**示例自己就违反了**（1/1/2 混装），而 B 的校验器直接拒绝。
    校验器比实现宽松，等于把问题留到交接时才发现。
    """

    def setUp(self):
        self.records = json.loads(VALID.read_text(encoding="utf-8"))

    def test_example_is_consistent(self):
        report = validate_records(self.records)
        self.assertTrue(report.ok, [str(i) for i in report.errors])

    # 2026-11-20：审查确认校验器曾漏掉“决定自身”和“决定对计划”的版次检查。
    # 回归恢复为强制断言，不能再以 expectedFailure 放行。
    def test_mixed_revisions_are_rejected(self):
        recs = copy.deepcopy(self.records)
        get(recs, "Decision", "det-demo-qr-01")["revision"] = 1
        report = validate_records(recs)
        codes = [i.code for i in report.errors]
        self.assertIn("decision_revision_mismatch", codes)

    def test_plan_revision_must_match(self):
        recs = copy.deepcopy(self.records)
        plan = get(recs, "RedactionPlan")
        plan["revision"] = 3
        report = validate_records(recs)
        codes = [i.code for i in report.errors]
        self.assertIn("decision_plan_revision_mismatch", codes)

    def test_migration_without_revision_is_rejected(self):
        migrated, _ = migrate_v3.migrate_records(
            [{"id": "det-demo-phone-01", "selected": True, "action": "hide"}]
        )
        recs = [
            r for r in copy.deepcopy(self.records)
            if not (r["record_type"] == "Decision" and r["finding_id"] == "det-demo-phone-01")
        ] + migrated
        report = validate_records(recs)
        self.assertIn("decision_revision_mismatch", [i.code for i in report.errors])


class SchemaSingleSource(unittest.TestCase):
    """三份 schema 必须字节一致——双源是 11-14 那次"示例互相被拒"的根因。"""

    def test_authority_and_vendored_are_identical(self):
        import hashlib
        authority = ROOT / "contract" / "schema" / "photocheck-v4.schema.json"
        vendored = ROOT / "app" / "vendor" / "photocheck" / "contract" / "schema" / "photocheck-v4.schema.json"
        if not vendored.is_file():
            self.skipTest("未 vendor 后端")
        self.assertEqual(
            hashlib.sha256(authority.read_bytes()).hexdigest(),
            hashlib.sha256(vendored.read_bytes()).hexdigest(),
            "schema 双源会再次导致跨仓库互拒",
        )

    def test_qr_transition_aliases_are_gone(self):
        """C-1：detailsQr 只允许 decodable / safe_domain / safe_ssid。"""
        schema = json.loads(
            (ROOT / "contract" / "schema" / "photocheck-v4.schema.json").read_text(encoding="utf-8")
        )
        props = set(schema["$defs"]["detailsQr"]["properties"])
        self.assertEqual(props, {"decodable", "safe_domain", "safe_ssid"})
        self.assertFalse(props & {"masked_summary", "domain", "ssid"})



class InteropWithB(unittest.TestCase):
    """双向互校：C 的示例过 B 的校验器，B 的示例过 C 的校验器。

    2026-11-14 外部审查指出"两份 schema 哈希不同、两边示例互相被拒"。
    只修一边不算修好——必须两个方向都钉住，否则下次再分叉还是没人发现。
    """

    def test_b_example_passes_c_validator(self):
        path = (ROOT / "app" / "vendor" / "photocheck" / "contract"
                / "examples" / "valid_records.json")
        if not path.is_file():
            self.skipTest("未 vendor B 的示例")
        records = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(records, dict):
            records = [records]
        report = validate_records(records)
        self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_c_example_passes_b_validator(self):
        """用 B 的 vendored 校验器反向验 C 的示例。

        B 的 `validation.py` 可能还没 vendor 进来（老包没有 contract/examples），
        缺失时跳过；有就必须过。
        """
        try:
            from app.vendor.photocheck.contract import validation as b_validation
        except Exception:
            self.skipTest("未 vendor B 的校验器")
        records = json.loads(VALID.read_text(encoding="utf-8"))
        try:
            b_validation.validate_records(records)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"B 的校验器拒绝了 C 的示例：{exc}")



class DisposalStateStages(unittest.TestCase):
    """K-09 阶段化（2026-11-15 共识）。

    `not_run` 与 `not_applicable` 必须拆开：
    - 缺失 check / not_run = 流水线没执行 → failed；
    - not_applicable = 检查器跑了但环境不可用 → pending。
    旧实现把两者都算 pending，报告漏一项看起来像"等待确认"。
    """

    def setUp(self):
        self.recs = json.loads(VALID.read_text(encoding="utf-8"))
        self.plan = get(self.recs, "RedactionPlan")
        self.report = get(self.recs, "VerificationReport")

    def state(self):
        return _derive_disposal_state(self.plan, self.report)

    def test_example_is_pending_due_to_face_not_applicable(self):
        # 示例里 manual-face-001 因缺人脸样本记为 not_applicable；
        # 按 K-09 这属于「检查器跑了但方法不可用」→ pending，不是 passed。
        # 2026-11-16 原断言写的是 passed，与示例数据不符，属测试过期。
        self.assertEqual(self.state(), "pending")

    def test_all_passed_when_no_check_is_pending(self):
        for check in self.report["checks"]:
            check["status"] = "passed"
        self.assertEqual(self.state(), "passed")

    def test_missing_check_is_failed(self):
        self.report["checks"] = [
            c for c in self.report["checks"] if c["region_id"] != "manual-face-001"
        ]
        self.assertEqual(self.state(), "failed", "报告漏了一项，不能算 pending")

    def test_not_run_is_failed(self):
        self.report["checks"][0]["status"] = "not_run"
        self.assertEqual(self.state(), "failed")

    def test_not_applicable_is_pending(self):
        self.report["checks"][0]["status"] = "not_applicable"
        self.assertEqual(self.state(), "pending")

    def test_failed_stays_failed(self):
        self.report["checks"][0]["status"] = "failed"
        self.assertEqual(self.state(), "failed")

    def test_empty_plan_is_not_applicable(self):
        self.plan["items"] = []
        self.assertEqual(self.state(), "not_applicable")

    def test_missing_check_against_passed_claim_is_mismatch(self):
        self.report["checks"] = [
            c for c in self.report["checks"] if c["region_id"] != "manual-face-001"
        ]
        report = validate_records(copy.deepcopy(self.recs))
        self.assertIn("disposal_state_mismatch", [i.code for i in report.errors])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class InteropWithA(unittest.TestCase):
    """A 的检测器输出必须能直接进我们的校验器（DR-002）。

    2026-11-02：A 交来的样例原本被拒——他用 safe_domain / safe_ssid 表达
    「K-06 要展示的域名与 SSID」，我们的 schema 只认 masked_summary / domain / ssid。
    两边都在猜字段名，规格卡只写了「域名 / SSID 等安全摘要」。已按 A 的方案补进
    schema（安全前缀更能表明"这是摘要不是原文"），并用这份样例做回归。
    """

    SAMPLE = ROOT / "contract" / "examples" / "a_side_sample.json"

    def test_a_side_sample_passes(self):
        import json

        data = json.loads(self.SAMPLE.read_text(encoding="utf-8"))
        report = validate_records([data["image_descriptor"], *data["detections"]])
        self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_wifi_payload_cannot_hide_in_safe_ssid(self):
        # schema 的 pattern 只禁控制字符，把整条 WIFI: payload 塞进 safe_ssid 仍能过形状检查
        rec = {
            "contract_version": "photocheck/v4", "record_type": "Detection",
            "id": "det-qr-001", "image_id": "img", "kind": "qr", "subtype": "qr_wifi",
            "bbox": [0, 0, 100, 100], "detector_score": None, "evidence": "qr_payload",
            "risk": 3, "details": {"decodable": True, "safe_ssid": "WIFI:S:home;P:secret;;"},
        }
        self.assertIn("payload_in_summary", codes(validate_records([rec])))

    def test_domain_with_scheme_rejected(self):
        rec = {
            "contract_version": "photocheck/v4", "record_type": "Detection",
            "id": "det-qr-001", "image_id": "img", "kind": "qr", "subtype": "qr_link",
            "bbox": [0, 0, 100, 100], "detector_score": None, "evidence": "qr_payload",
            "risk": 1, "details": {"decodable": True, "safe_domain": "https://example.com/x"},
        }
        report = validate_records([rec])
        self.assertFalse(report.ok)
        self.assertIn("payload_in_summary", codes(report))
