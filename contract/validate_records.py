#!/usr/bin/env python3
"""PhotoCheck 契约 v4 记录校验器。

用法：
    python contract/validate_records.py record.json                 # 单条
    python contract/validate_records.py records.json                # 数组，或 {"records": [...]}
    python contract/validate_records.py a.json b.json --json       # 多条文件合并校验

作为库使用：
    from contract.validate_records import validate_records
    report = validate_records(records)
    report.raise_if_invalid()

两层检查：
  1. 结构层——按 schema/photocheck-v4.schema.json 逐条校验。有 jsonschema 时用它，
     没有时用本文件内置的同子集实现（覆盖本 schema 用到的全部关键字，遇到不支持
     的关键字直接报错，绝不静默放过）。
  2. 跨记录层——JSON Schema 表达不了的引用完整性、revision 绑定、disposal_state
     推导、泄漏红线扫描。

只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

CONTRACT_VERSION = "photocheck/v4"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "photocheck-v4.schema.json"

RECORD_TYPES = (
    "ImageDescriptor",
    "Detection",
    "Decision",
    "RedactionPlan",
    "VerificationReport",
)

# 泄漏红线：任何可序列化记录里出现这些字段都直接判错（规格卡 §0.2「泄漏与迁移红线」）
FORBIDDEN_KEYS = frozenset(
    {
        "text_ref",
        "payload_ref",
        "raw_exif",
        "raw_text",
        "raw_payload",
        "exif_raw",
        "payload",
        "original_text",
        "thumbnail_bytes",
        "thumbnail_blob",
        "raw_bytes",
        "base64_image",
        "raw_value",        # 会议手册草案里的字段名；规格卡禁止它存在，哪怕是 null
    }
)

FULL_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
FULL_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")


# --------------------------------------------------------------------------
# 问题与报告
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """一条校验问题。code 供脚本判定，message 给人看。"""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


@dataclass
class ValidationReport:
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)
    record_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append(Issue(code, path, message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append(Issue(code, path, message))

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise ContractError(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "record_count": self.record_count,
            "errors": [i.__dict__ for i in self.errors],
            "warnings": [i.__dict__ for i in self.warnings],
        }


class ContractError(ValueError):
    """校验不通过。args[0] 是 ValidationReport。"""

    @property
    def report(self) -> ValidationReport:
        return self.args[0]

    def __str__(self) -> str:
        rep: ValidationReport = self.args[0]
        head = f"契约 v4 校验失败：{len(rep.errors)} 个错误"
        body = "\n".join("  " + str(i) for i in rep.errors[:20])
        more = "" if len(rep.errors) <= 20 else f"\n  ……另有 {len(rep.errors) - 20} 条"
        return head + "\n" + body + more


class UnsupportedSchema(ValueError):
    """schema 里出现了内置校验器不认识的关键字——宁可报错，不可静默放过。"""


# --------------------------------------------------------------------------
# 结构层：JSON Schema 子集校验器
# --------------------------------------------------------------------------

_SUPPORTED = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "title",
        "description",
        "oneOf",
        "allOf",
        "if",
        "then",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
    }
)

_TYPE_CHECKS = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
}


@lru_cache(maxsize=4)
def _read_schema(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_schema(path: Optional[Path] = None) -> Dict[str, Any]:
    """读 schema。**结果按路径缓存**——批量校验时每条记录重读 46 KB 纯属浪费，
    而 schema 在进程生命周期内不会变。需要热更新就换进程。"""
    return _read_schema(Path(path) if path else SCHEMA_PATH)


def assert_supported(schema: Mapping[str, Any], _where: str = "$") -> None:
    """递归确认内置校验器能完整表达这份 schema。"""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key.startswith("x-"):
                continue
            if key not in _SUPPORTED:
                raise UnsupportedSchema(f"{_where}: 不支持的关键字 {key!r}")
            if key in ("$defs", "properties"):
                for name, sub in value.items():
                    assert_supported(sub, f"{_where}/{key}/{name}")
            elif key in ("oneOf", "allOf"):
                for idx, sub in enumerate(value):
                    assert_supported(sub, f"{_where}/{key}/{idx}")
            elif key in ("if", "then", "items", "additionalProperties"):
                if isinstance(value, dict):
                    assert_supported(value, f"{_where}/{key}")
            elif key == "prefixItems":
                for idx, sub in enumerate(value):
                    assert_supported(sub, f"{_where}/prefixItems/{idx}")


class _SchemaChecker:
    def __init__(self, schema: Mapping[str, Any]):
        self.root = schema

    def _resolve(self, ref: str) -> Mapping[str, Any]:
        if not ref.startswith("#/"):
            raise UnsupportedSchema(f"只支持文档内 $ref：{ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def validate(self, instance: Any, schema: Optional[Mapping[str, Any]] = None) -> List[Issue]:
        issues: List[Issue] = []
        self._check(instance, schema or self.root, "$", issues)
        return issues

    # -- 内部 --------------------------------------------------------------

    def _matches(self, instance: Any, schema: Mapping[str, Any], path: str) -> bool:
        probe: List[Issue] = []
        self._check(instance, schema, path, probe)
        return not probe

    def _check(
        self, inst: Any, schema: Any, path: str, out: List[Issue]
    ) -> None:
        if not isinstance(schema, dict):
            return
        if schema is True or schema == {}:
            return
        if schema is False:
            out.append(Issue("schema_false", path, "该位置不允许出现任何值"))
            return

        if "$ref" in schema:
            self._check(inst, self._resolve(schema["$ref"]), path, out)

        if "type" in schema:
            allowed = schema["type"]
            names = allowed if isinstance(allowed, list) else [allowed]
            if not any(_TYPE_CHECKS[n](inst) for n in names):
                out.append(
                    Issue(
                        "type_mismatch",
                        path,
                        f"类型应为 {'/'.join(names)}，实际是 {type(inst).__name__}",
                    )
                )
                return

        if "const" in schema and inst != schema["const"]:
            out.append(
                Issue("const_mismatch", path, f"应为 {schema['const']!r}，实际 {inst!r}")
            )

        if "enum" in schema and inst not in schema["enum"]:
            out.append(
                Issue(
                    "enum_mismatch",
                    path,
                    f"{inst!r} 不在允许取值 {schema['enum']} 内",
                )
            )

        if isinstance(inst, str):
            if "minLength" in schema and len(inst) < schema["minLength"]:
                out.append(
                    Issue("min_length", path, f"长度 {len(inst)} 小于下限 {schema['minLength']}")
                )
            if "maxLength" in schema and len(inst) > schema["maxLength"]:
                out.append(
                    Issue("max_length", path, f"长度 {len(inst)} 大于上限 {schema['maxLength']}")
                )
            if "pattern" in schema and re.search(schema["pattern"], inst) is None:
                out.append(
                    Issue("pattern_mismatch", path, f"{inst!r} 不匹配 {schema['pattern']!r}")
                )

        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in schema and inst < schema["minimum"]:
                out.append(
                    Issue("minimum", path, f"{inst} 小于下限 {schema['minimum']}")
                )
            if "maximum" in schema and inst > schema["maximum"]:
                out.append(
                    Issue("maximum", path, f"{inst} 大于上限 {schema['maximum']}")
                )

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                out.append(
                    Issue("min_items", path, f"元素数 {len(inst)} 小于下限 {schema['minItems']}")
                )
            if "maxItems" in schema and len(inst) > schema["maxItems"]:
                out.append(
                    Issue("max_items", path, f"元素数 {len(inst)} 大于上限 {schema['maxItems']}")
                )
            if "prefixItems" in schema:
                for idx, sub in enumerate(schema["prefixItems"]):
                    if idx < len(inst):
                        self._check(inst[idx], sub, f"{path}[{idx}]", out)
            if isinstance(schema.get("items"), dict):
                start = len(schema.get("prefixItems", []))
                for idx in range(start, len(inst)):
                    self._check(inst[idx], schema["items"], f"{path}[{idx}]", out)

        if isinstance(inst, dict):
            for key in schema.get("required", []):
                if key not in inst:
                    out.append(
                        Issue("missing_field", f"{path}.{key}", f"缺少必填字段 {key!r}")
                    )
            props = schema.get("properties", {})
            for key, value in inst.items():
                if key in props:
                    self._check(value, props[key], f"{path}.{key}", out)
                elif schema.get("additionalProperties") is False:
                    out.append(
                        Issue(
                            "unknown_field",
                            f"{path}.{key}",
                            f"不允许的字段 {key!r}（契约禁止未知字段）",
                        )
                    )

        for idx, sub in enumerate(schema.get("allOf", [])):
            self._check(inst, sub, path, out)

        if "if" in schema and "then" in schema:
            if self._matches(inst, schema["if"], path):
                self._check(inst, schema["then"], path, out)

        if "oneOf" in schema:
            hits = sum(1 for sub in schema["oneOf"] if self._matches(inst, sub, path))
            if hits != 1:
                out.append(
                    Issue(
                        "one_of",
                        path,
                        f"oneOf 命中 {hits} 个分支，应恰好 1 个",
                    )
                )


def _target_ref(record: Any, schema: Mapping[str, Any]) -> Optional[str]:
    """按 record_type 定位到 $defs 里的分支。

    根 schema 的 oneOf 只能给出「五类都不匹配」这种没有定位信息的结论；
    先按 record_type 派发，报错才能精确到字段。两者等价：唯一性与 const
    record_type + additionalProperties=false 共同保证一条记录只可能命中一个分支。
    """
    if isinstance(record, Mapping):
        rt = record.get("record_type")
        defs = schema.get("$defs", {})
        if isinstance(rt, str) and rt in defs:
            return f"#/$defs/{rt}"
    return None


_VALIDATOR_CACHE: Dict[int, Any] = {}


def _validator_for(schema: Mapping[str, Any]) -> Any:
    """按 schema 缓存 validator：check_schema 很贵，批量校验时不该每张图重跑一次。

    缓存里同时保留 schema 引用，避免 id() 被回收后复用到别的对象上。
    """
    key = id(schema)
    cached = _VALIDATOR_CACHE.get(key)
    if cached is not None and cached[0] is schema:
        return cached[1]
    import jsonschema  # type: ignore

    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    _VALIDATOR_CACHE[key] = (schema, validator)
    return validator


def check_structure(record: Any, schema: Mapping[str, Any], label: str) -> List[Issue]:
    ref = _target_ref(record, schema)
    try:
        import jsonschema  # type: ignore  # noqa: F401
    except ImportError:
        target = _SchemaChecker(schema)._resolve(ref) if ref else schema
        issues = _SchemaChecker(schema).validate(record, target)
        return [Issue(i.code, f"{label}{i.path[1:]}", i.message) for i in issues]

    validator = _validator_for(schema)
    # 用 descend 而不是新建 validator：$ref 必须以整份文档为基准解析
    errors = validator.descend(record, {"$ref": ref}) if ref else validator.iter_errors(record)
    issues = []
    for err in sorted(errors, key=lambda e: list(e.absolute_path)):
        loc = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        # jsonschema 把「多了字段」「少了字段」都报在对象本身，这里补上字段名，方便定位
        if err.validator in ("additionalProperties", "required"):
            names = re.findall(r"'([^']+)'", err.message)
            loc += "".join(f".{name}" for name in names)
        issues.append(Issue("schema", f"{label}{loc}", err.message))
    return issues


# --------------------------------------------------------------------------
# 泄漏红线扫描
# --------------------------------------------------------------------------


def scan_leaks(instance: Any, label: str, out: List[Issue], path: str = "") -> None:
    if isinstance(instance, dict):
        for key, value in instance.items():
            here = f"{path}.{key}" if path else key
            if key in FORBIDDEN_KEYS:
                out.append(
                    Issue(
                        "forbidden_key",
                        f"{label}.{here}",
                        f"字段 {key!r} 属于原始敏感数据，禁止进入可序列化记录",
                    )
                )
            scan_leaks(value, label, out, here)
    elif isinstance(instance, list):
        for idx, value in enumerate(instance):
            scan_leaks(value, label, out, f"{path}[{idx}]")


def scan_qr_details(det: Mapping[str, Any], label: str, out: List[Issue]) -> None:
    """二维码的安全摘要不能变成 payload 的搬运工。

    `safe_ssid` 允许任意非控制字符，所以 `WIFI:S:home;P:secret;;` 能通过 schema 的
    pattern——那等于把 Wi-Fi 密码写进了可序列化记录。schema 管形状，这里管内容。
    """
    details = det.get("details") or {}
    ssid = details.get("safe_ssid")
    if isinstance(ssid, str):
        stripped = ssid.strip()
        if ";" in stripped or stripped.upper().startswith("WIFI:"):
            out.append(Issue(
                "payload_in_summary", f"{label}.details.safe_ssid",
                "safe_ssid 疑似 Wi-Fi payload 原文（含 ; 或 WIFI: 前缀），只允许 SSID 本身",
            ))
    domain = details.get("safe_domain")
    if isinstance(domain, str) and ("://" in domain or "/" in domain):
        out.append(Issue(
            "payload_in_summary", f"{label}.details.safe_domain",
            "safe_domain 含协议或路径，只允许域名",
        ))


def scan_masked_preview(det: Mapping[str, Any], label: str, out: List[Issue]) -> None:
    details = det.get("details") or {}
    preview = details.get("masked_preview")
    if not isinstance(preview, str):
        return
    subtype = det.get("subtype")
    if subtype == "phone" and FULL_PHONE_RE.search(preview):
        out.append(
            Issue(
                "unmasked_preview",
                f"{label}.details.masked_preview",
                "手机号预览里出现完整 11 位号码，掩码必须留 *",
            )
        )
    if subtype == "idcard" and FULL_IDCARD_RE.search(preview):
        out.append(
            Issue(
                "unmasked_preview",
                f"{label}.details.masked_preview",
                "身份证预览里出现完整 18 位号码，掩码必须留 *",
            )
        )


# --------------------------------------------------------------------------
# 跨记录不变量
# --------------------------------------------------------------------------


def _current_decisions(decisions: Mapping[str, List[Mapping[str, Any]]]) -> Dict[str, Mapping[str, Any]]:
    """每个 finding 取 revision 最大的那条决定为当前决定。"""
    return {fid: max(rows, key=lambda r: r["revision"]) for fid, rows in decisions.items()}


def _derive_disposal_state(plan: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    items = plan.get("items") or []
    if not items:
        return "not_applicable"
    status: Dict[str, str] = {}
    for check in report.get("checks") or []:
        status.setdefault(check["region_id"], check["status"])
    item_statuses = [status.get(item["id"], "not_run") for item in items]
    if any(s in {"failed", "not_run"} for s in item_statuses):
        return "failed"
    if any(s == "not_applicable" for s in item_statuses):
        return "pending"
    if all(s == "passed" for s in item_statuses):
        return "passed"
    return "failed"


def validate_cross_record(records: Sequence[Mapping[str, Any]], report: ValidationReport) -> None:
    descriptors: Dict[str, Mapping[str, Any]] = {}
    detections: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    finding_to_image: Dict[str, set] = {}
    decisions: Dict[str, List[Mapping[str, Any]]] = {}
    plans: Dict[str, List[Mapping[str, Any]]] = {}
    reports: List[Mapping[str, Any]] = []

    for rec in records:
        rt = rec["record_type"]
        label = f"{rt}({rec.get('image_id') or rec.get('id') or rec.get('finding_id') or '?'})"
        if rt == "ImageDescriptor":
            image_id = rec["image_id"]
            if image_id in descriptors:
                report.error(
                    "duplicate_image_descriptor",
                    label,
                    f"同一 image_id 出现多条 ImageDescriptor：{image_id}",
                )
            descriptors[image_id] = rec
        elif rt == "Detection":
            bucket = detections.setdefault(rec["image_id"], {})
            if rec["id"] in bucket:
                report.error(
                    "duplicate_detection_id",
                    label,
                    f"image {rec['image_id']} 内 Detection.id 重复：{rec['id']}",
                )
            bucket[rec["id"]] = rec
            finding_to_image.setdefault(rec["id"], set()).add(rec["image_id"])
        elif rt == "Decision":
            decisions.setdefault(rec["finding_id"], []).append(rec)
        elif rt == "RedactionPlan":
            plans.setdefault(rec["image_id"], []).append(rec)
        elif rt == "VerificationReport":
            reports.append(rec)

    # D1 同一 finding_id 跨多张图：规格卡 0.2 只要求「在一个 image_id 内唯一」，
    # 所以这不算违约，只是批量处理时 Decision 会失去消歧能力 —— 记 warning。
    for fid, images in finding_to_image.items():
        if len(images) > 1:
            report.warn(
                "ambiguous_finding_id",
                f"Detection({fid})",
                f"finding_id 同时出现在 {sorted(images)}；Decision 只带 finding_id，无法判断属于哪张图。"
                "建议把 image_id 编进 id，例如 det-<image>-<kind>-<n>",
            )

    # D2 bbox 必须落在工作图内
    for image_id, bucket in detections.items():
        desc = descriptors.get(image_id)
        if not desc:
            continue
        for det_id, det in bucket.items():
            bbox = det.get("bbox")
            if not bbox:
                continue
            x, y, w, h = bbox
            if x + w > desc["width"] or y + h > desc["height"]:
                report.error(
                    "bbox_out_of_bounds",
                    f"Detection({det_id}).bbox",
                    f"{bbox} 超出工作图 {desc['width']}x{desc['height']}",
                )

    # D3 Decision 必须指向存在的 Detection，且不得指向 meta
    current = _current_decisions(decisions)
    for fid, rows in decisions.items():
        revisions = [r["revision"] for r in rows]
        if len(set(revisions)) != len(revisions):
            report.error(
                "duplicate_decision_revision",
                f"Decision({fid})",
                f"同一 finding 出现重复 revision：{sorted(revisions)}",
            )
        owners = finding_to_image.get(fid)
        if not owners:
            report.error(
                "decision_orphan",
                f"Decision({fid})",
                "找不到对应的 Detection，决定无法落地",
            )
            continue
        image_id = sorted(owners)[0]
        det = detections.get(image_id, {}).get(fid)
        if det and det.get("kind") == "meta":
            report.error(
                "decision_on_metadata",
                f"Decision({fid})",
                "元数据整图自动清除，不接受 Decision；该 Finding 仅用于告知",
            )

    # D3b 同一张图的当前决定必须共享一个全局版次。
    # Decision 记录本身不带 image_id，所以按 Detection 归属把当前决定投回图片。
    revisions_by_image: Dict[str, set] = {}
    for fid, decision in current.items():
        for image_id in finding_to_image.get(fid) or set():
            revisions_by_image.setdefault(image_id, set()).add(decision["revision"])
    for image_id, revisions in revisions_by_image.items():
        if len(revisions) > 1:
            report.error(
                "decision_revision_mismatch",
                f"Decision({image_id})",
                f"同一张图的当前 Decision 混用了多个 revision：{sorted(revisions)}；"
                "任何决定变化都必须让整张图的当前决定一起递增",
            )

    # D4 计划项与 redact 决定必须一一对应
    def plan_of(image_id: str) -> Optional[Mapping[str, Any]]:
        rows = plans.get(image_id)
        if not rows:
            return None
        return max(rows, key=lambda r: r["revision"])

    for image_id, rows in plans.items():
        if len(rows) > 1:
            report.warn(
                "multiple_plans",
                f"RedactionPlan({image_id})",
                f"存在 {len(rows)} 份计划，按 revision 最大者为准",
            )
        plan = plan_of(image_id)
        assert plan is not None
        image_decision_revisions = {
            current[fid]["revision"]
            for fid, owners in finding_to_image.items()
            if image_id in owners and fid in current
        }
        if image_decision_revisions - {plan["revision"]}:
            report.error(
                "decision_plan_revision_mismatch",
                f"RedactionPlan({image_id})",
                f"计划 revision={plan['revision']} 与当前 Decision revision="
                f"{sorted(image_decision_revisions)} 不一致；旧计划不能承载新决定",
            )
        seen_ids: set = set()
        seen_findings: set = set()
        for item in plan.get("items") or []:
            if item["id"] in seen_ids:
                report.error(
                    "duplicate_plan_item_id",
                    f"RedactionPlan({image_id}).items",
                    f"计划区域 id 重复：{item['id']}",
                )
            seen_ids.add(item["id"])
            fid = item["finding_id"]
            if fid is None:
                continue
            if fid in seen_findings:
                report.error(
                    "duplicate_plan_item_for_finding",
                    f"RedactionPlan({image_id}).items",
                    f"finding {fid} 对应多个计划区域，redact 决定必须一一对应",
                )
            seen_findings.add(fid)
            decision = current.get(fid)
            if decision is None:
                report.error(
                    "plan_item_without_redact_decision",
                    f"RedactionPlan({image_id}).items[{item['id']}]",
                    f"finding {fid} 没有 Decision，计划区域无来源",
                )
            elif decision["action"] != "redact":
                report.error(
                    "plan_item_without_redact_decision",
                    f"RedactionPlan({image_id}).items[{item['id']}]",
                    f"finding {fid} 的当前决定是 retain，不应出现在计划里",
                )
            det = detections.get(image_id, {}).get(fid)
            if det and det.get("kind") == "qr" and item["method"] != "solid":
                report.error(
                    "qr_method_not_solid",
                    f"RedactionPlan({image_id}).items[{item['id']}].method",
                    f"二维码区域必须实心遮盖，当前为 {item['method']}；QR 有纠错能力，半遮可能仍可解",
                )
            if det and det.get("kind") == "face" and item["method"] != "solid":
                report.error(
                    "face_method_not_solid",
                    f"RedactionPlan({image_id}).items[{item['id']}].method",
                    f"人脸区域必须实心遮盖，当前为 {item['method']}；"
                    "模糊和像素化不能作为不可逆匿名化保证",
                )
            desc = descriptors.get(image_id)
            if desc:
                x, y, w, h = item["bbox"]
                if x + w > desc["width"] or y + h > desc["height"]:
                    report.error(
                        "bbox_out_of_bounds",
                        f"RedactionPlan({image_id}).items[{item['id']}].bbox",
                        f"{item['bbox']} 超出工作图 {desc['width']}x{desc['height']}",
                    )

    for fid, decision in current.items():
        if decision["action"] != "redact":
            continue
        owners = finding_to_image.get(fid) or set()
        for image_id in owners:
            plan = plan_of(image_id)
            if plan is None:
                report.error(
                    "redact_without_plan_item",
                    f"Decision({fid})",
                    "决定为 redact，但该 image 没有 RedactionPlan",
                )
                break
            if not any(i["finding_id"] == fid for i in plan.get("items") or []):
                report.error(
                    "redact_without_plan_item",
                    f"Decision({fid})",
                    "决定为 redact，但计划里没有对应区域",
                )
                break

    # D5 复检报告必须绑定当前计划
    for rep in reports:
        image_id = rep["image_id"]
        label = f"VerificationReport({image_id})"
        plan = plan_of(image_id)
        if plan is None:
            report.error(
                "report_without_plan",
                label,
                "没有对应的 RedactionPlan，复检无对象",
            )
            continue
        if rep["plan_revision"] != plan["revision"]:
            report.error(
                "stale_verification",
                label,
                f"plan_revision={rep['plan_revision']} 与当前计划 revision={plan['revision']} 不一致，"
                "该结论已失效，不得展示为当前结果",
            )
        item_ids = {i["id"] for i in plan.get("items") or []}
        seen_regions: set = set()
        status_by_region: Dict[str, str] = {}
        for check in rep.get("checks") or []:
            rid = check["region_id"]
            if rid in seen_regions:
                report.error(
                    "duplicate_check_region",
                    f"{label}.checks[{rid}]",
                    "同一区域出现多个复检结果，只允许一个当前结果",
                )
            seen_regions.add(rid)
            status_by_region.setdefault(rid, check["status"])
            if rid not in item_ids:
                report.error(
                    "check_region_orphan",
                    f"{label}.checks[{rid}]",
                    "复检区域不在当前 RedactionPlan 内",
                )

        missing_checks = item_ids - seen_regions
        if missing_checks:
            report.error(
                "missing_verification_check",
                label,
                f"每个计划项必须恰有一条 check，缺少：{sorted(missing_checks)}",
            )

        for res in rep.get("residuals") or []:
            fid = res.get("finding_id")
            rid = res.get("region_id")
            if res["reason"] == "retained":
                decision = current.get(fid) if fid else None
                if decision is None or decision["action"] != "retain":
                    report.error(
                        "residual_retained_orphan",
                        f"{label}.residuals[{fid or rid}]",
                        "reason=retained 必须对应一条 action=retain 的当前决定",
                    )
            else:  # verification_failed
                if rid is None:
                    report.error(
                        "residual_missing_region",
                        f"{label}.residuals[{fid}]",
                        "verification_failed 必须给出 region_id",
                    )
                elif status_by_region.get(rid) not in {"failed", "not_run"}:
                    report.error(
                        "residual_without_failed_check",
                        f"{label}.residuals[{rid}]",
                        "verification_failed 对应的区域复检结果不是 failed",
                    )

        for rid, status in status_by_region.items():
            if status not in {"failed", "not_run"}:
                continue
            if not any(
                (r.get("region_id") == rid) and r["reason"] == "verification_failed"
                for r in rep.get("residuals") or []
            ):
                report.error(
                    "failed_check_without_residual",
                    f"{label}.checks[{rid}]",
                    "区域复检 failed/not_run，必须写入 residuals(reason=verification_failed)",
                )

        expected = _derive_disposal_state(plan, rep)
        if rep["disposal_state"] != expected:
            report.error(
                "disposal_state_mismatch",
                f"{label}.disposal_state",
                f"按当前 checks 推导应为 {expected}，实际写了 {rep['disposal_state']}",
            )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def validate_records(
    records: Any,
    *,
    schema: Optional[Mapping[str, Any]] = None,
    scan_secrets: bool = True,
) -> ValidationReport:
    """校验一组（或单条）契约 v4 记录。永不抛异常，结论全在返回值里。"""
    if isinstance(records, Mapping):
        batch: List[Any] = [records]
    else:
        batch = list(records)

    report = ValidationReport(record_count=len(batch))
    schema = schema or load_schema()
    assert_supported(schema)

    ok_records: List[Mapping[str, Any]] = []
    for idx, rec in enumerate(batch):
        label = f"#{idx}"
        if not isinstance(rec, Mapping):
            report.error("not_an_object", label, "记录必须是 JSON 对象")
            continue
        rt = rec.get("record_type")
        if rt in RECORD_TYPES:
            label = f"{rt}({rec.get('image_id') or rec.get('id') or rec.get('finding_id') or idx})"
        # 泄漏红线独立于结构校验：即使字段名不合法也要先报出来，不能被 unknown_field 盖掉
        if scan_secrets:
            leak_sink: List[Issue] = []
            scan_leaks(rec, label, leak_sink)
            report.errors.extend(leak_sink)
            if rt == "Detection":
                preview_sink: List[Issue] = []
                scan_masked_preview(rec, label, preview_sink)
                scan_qr_details(rec, label, preview_sink)
                report.errors.extend(preview_sink)
        structural = check_structure(rec, schema, label)
        if structural:
            report.errors.extend(structural)
            continue
        ok_records.append(rec)

    # 结构不通过的记录不参与跨记录检查，避免一处笔误炸出一串噪声
    if len(ok_records) == len(batch):
        validate_cross_record(ok_records, report)

    return report


def load_records(path: Path) -> List[Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "records" in data:
        return list(data["records"])
    if isinstance(data, list):
        return data
    return [data]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校验 PhotoCheck 契约 v4 记录")
    parser.add_argument("files", nargs="+", help="JSON 文件（单条 / 数组 / {records:[...]}）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--strict", action="store_true", help="把 warning 也算失败")
    args = parser.parse_args(argv)

    records: List[Any] = []
    for name in args.files:
        try:
            records.extend(load_records(Path(name)))
        except FileNotFoundError:
            print(f"文件不存在：{name}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            # CLI 是给别人用的入口，堆栈不如一行话说得清楚
            print(f"{name} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}",
                  file=sys.stderr)
            return 2

    report = validate_records(records)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"校验 {report.record_count} 条记录：{len(report.errors)} 个错误，"
              f"{len(report.warnings)} 个警告")
        for issue in report.errors:
            print("  错误 " + str(issue))
        for issue in report.warnings:
            print("  警告 " + str(issue))

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
