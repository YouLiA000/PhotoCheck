"""Validation and cross-record invariants for PhotoCheck contract v4."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - exercised only without dev deps
    raise ImportError(
        "PhotoCheck contract validation requires jsonschema. "
        "Install photocheck/contract/requirements-contract.txt."
    ) from exc

CONTRACT_VERSION = "photocheck/v4"
SCHEMA_PATH = Path(__file__).with_name("schema") / "photocheck-v4.schema.json"

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
        "raw_value",
    }
)
FULL_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
FULL_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")


class ContractError(ValueError):
    """Raised when a record is malformed or violates a workflow invariant."""


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _validator() -> jsonschema.validators.Validator:
    """Compile and meta-check the immutable schema once per process."""

    schema = load_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def validate_record(record: Mapping[str, Any], *, image: Mapping[str, Any] | None = None) -> None:
    """Validate one record against the JSON Schema and optional image bounds."""

    _scan_serializable_secrets(record)
    try:
        _validator().validate(dict(record))
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = f" at {path}" if path else ""
        raise ContractError(f"invalid PhotoCheck v4 record{location}: {exc.message}") from exc

    if image is not None:
        _validate_bbox_bounds(record, image)


def validate_records(
    records: Iterable[Mapping[str, Any]],
    *,
    image: Mapping[str, Any] | None = None,
) -> None:
    """Validate a workflow snapshot, including IDs, revisions and references."""

    records = [dict(record) for record in records]
    descriptors = [record for record in records if record.get("record_type") == "ImageDescriptor"]
    if image is not None:
        validate_record(image)
        if descriptors and descriptors != [dict(image)]:
            raise ContractError("image descriptor does not match the supplied image")
        descriptors = [dict(image)]
    if len(descriptors) > 1:
        raise ContractError("a workflow snapshot must contain at most one ImageDescriptor")

    for record in records:
        validate_record(record, image=descriptors[0] if descriptors else None)

    image_id = descriptors[0]["image_id"] if descriptors else None
    for record in records:
        if "image_id" in record and image_id is not None and record["image_id"] != image_id:
            raise ContractError("records from different images cannot share a snapshot")

    detections = [record for record in records if record.get("record_type") == "Detection"]
    decisions = [record for record in records if record.get("record_type") == "Decision"]
    plans = [record for record in records if record.get("record_type") == "RedactionPlan"]
    reports = [record for record in records if record.get("record_type") == "VerificationReport"]

    _require_unique(detections, "id", "detection id")
    detection_by_id = {record["id"]: record for record in detections}

    if len(plans) > 1 or len(reports) > 1:
        raise ContractError("a workflow snapshot may contain only one plan and one report")

    decision_ids = [record["finding_id"] for record in decisions]
    if len(decision_ids) != len(set(decision_ids)):
        raise ContractError("a finding may have only one current decision")
    decision_revisions = {decision["revision"] for decision in decisions}
    if len(decision_revisions) > 1:
        raise ContractError("all current decisions must share one revision")

    for decision in decisions:
        finding = detection_by_id.get(decision["finding_id"])
        if finding is None:
            raise ContractError(f"decision references unknown finding {decision['finding_id']}")
        if finding["kind"] == "meta":
            raise ContractError("metadata is always removed and cannot have a Decision")

    actionable_ids = {
        detection["id"] for detection in detections if detection["kind"] != "meta"
    }
    if plans and set(decision_ids) != actionable_ids:
        raise ContractError("every non-metadata Detection requires exactly one Decision")
    if reports and not plans:
        raise ContractError("a VerificationReport requires its RedactionPlan")

    if plans:
        plan = plans[0]
        plan_revision = plan["revision"]
        if decision_revisions - {plan_revision}:
            raise ContractError("all decisions and the redaction plan must share one revision")

        items = plan["items"]
        _require_unique(items, "id", "redaction item id")
        redact_ids = {
            decision["finding_id"]
            for decision in decisions
            if decision["action"] == "redact"
        }
        item_finding_ids = {
            item["finding_id"] for item in items if item["finding_id"] is not None
        }
        if redact_ids != item_finding_ids:
            raise ContractError("redaction plan must contain exactly one item per redact decision")

        for item in items:
            finding_id = item["finding_id"]
            if finding_id is None:
                continue
            finding = detection_by_id.get(finding_id)
            if finding is None:
                raise ContractError(f"redaction item references unknown finding {finding_id}")
            if item["bbox"] != finding["bbox"]:
                raise ContractError("redaction item bbox must match its Detection bbox")

        if reports:
            report = reports[0]
            if report["plan_revision"] != plan_revision:
                raise ContractError("verification report is stale for this redaction plan")
            region_ids = {item["id"] for item in items}
            check_ids = [check["region_id"] for check in report["checks"]]
            if len(check_ids) != len(set(check_ids)):
                raise ContractError("each redaction item may have only one verification check")
            unknown_checks = set(check_ids) - region_ids
            if unknown_checks:
                raise ContractError(f"verification checks reference unknown regions: {sorted(unknown_checks)}")
            missing_checks = region_ids - set(check_ids)
            if missing_checks:
                raise ContractError(
                    f"verification report is missing checks for regions: {sorted(missing_checks)}"
                )
            expected_state = compute_disposal_state(plan, report["checks"])
            if report["disposal_state"] != expected_state:
                raise ContractError(
                    f"disposal_state must be {expected_state!r}, "
                    f"not {report['disposal_state']!r}"
                )


def compute_disposal_state(
    plan: Mapping[str, Any],
    checks: Iterable[Mapping[str, Any]],
) -> str:
    """Derive the only valid aggregate state for a redaction plan."""

    items = list(plan.get("items", []))
    if not items:
        return "not_applicable"

    by_region = {check["region_id"]: check["status"] for check in checks}
    statuses = [by_region.get(item["id"], "not_run") for item in items]
    if any(status in {"failed", "not_run"} for status in statuses):
        return "failed"
    if any(status == "not_applicable" for status in statuses):
        return "pending"
    if any(status != "passed" for status in statuses):
        return "failed"
    return "passed"


def _validate_bbox_bounds(record: Mapping[str, Any], image: Mapping[str, Any]) -> None:
    width = image["width"]
    height = image["height"]
    bboxes: list[tuple[str, list[int]]] = []

    if record.get("record_type") == "Detection" and record.get("bbox") is not None:
        bboxes.append(("bbox", record["bbox"]))
    if record.get("record_type") == "RedactionPlan":
        bboxes.extend((f"items[{index}].bbox", item["bbox"]) for index, item in enumerate(record["items"]))

    for path, bbox in bboxes:
        x, y, box_width, box_height = bbox
        if x + box_width > width or y + box_height > height:
            raise ContractError(f"{path} must stay inside the canonical image bounds")


def _require_unique(records: Iterable[Mapping[str, Any]], key: str, label: str) -> None:
    values = [record[key] for record in records]
    if len(values) != len(set(values)):
        raise ContractError(f"{label} values must be unique within a workflow snapshot")


def _scan_serializable_secrets(instance: Any, path: str = "") -> None:
    """Reject raw-sensitive fields and unmasked values before schema validation.

    JSON Schema can enforce field names and simple shapes, but it cannot tell
    that a superficially valid preview contains a complete phone number. This
    scan mirrors the shared C-side checker so a record cannot pass one runtime
    and fail the other.
    """

    if isinstance(instance, Mapping):
        for key, value in instance.items():
            here = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_KEYS:
                raise ContractError(
                    f"forbidden raw-sensitive field at {here}: {key!r}"
                )
            if key == "safe_ssid" and isinstance(value, str):
                stripped = value.strip()
                if ";" in stripped or stripped.upper().startswith("WIFI:"):
                    raise ContractError(
                        f"safe_ssid at {here} looks like a raw Wi-Fi payload"
                    )
            if key == "safe_domain" and isinstance(value, str):
                if "://" in value or "/" in value:
                    raise ContractError(
                        f"safe_domain at {here} contains a scheme or path"
                    )
            _scan_serializable_secrets(value, here)
        return

    if isinstance(instance, (list, tuple)):
        for index, value in enumerate(instance):
            _scan_serializable_secrets(value, f"{path}[{index}]")
        return

    if isinstance(instance, str):
        if FULL_PHONE_RE.search(instance):
            raise ContractError(f"complete phone number is forbidden at {path or '$'}")
        if FULL_IDCARD_RE.search(instance):
            raise ContractError(f"complete ID-card number is forbidden at {path or '$'}")
