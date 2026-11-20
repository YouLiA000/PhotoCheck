#!/usr/bin/env python3
"""v3 历史记录 → 契约 v4 记录的迁移。

规格卡 §0.2「泄漏与迁移红线」只允许无冲突迁移：

    selected=true  + action=hide          → action=redact
    selected=false + action=keep | skip   → action=retain
    其余组合                              → 拒绝

另外：含 text_ref / payload_ref / raw_exif 明文的历史记录直接拒绝；
旧 verify_status 一律丢弃且不受信任，迁移出的计划必须重新复检。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Tuple

CONTRACT_VERSION = "photocheck/v4"

REJECTED_KEYS = ("text_ref", "payload_ref", "raw_exif", "raw_text", "raw_payload", "payload")

_ALLOWED = {
    (True, "hide"): "redact",
    (False, "keep"): "retain",
    (False, "skip"): "retain",
}


class MigrationRefused(ValueError):
    """该条历史记录无法无冲突迁移。"""


def migrate_decision(v3: Mapping[str, Any]) -> Dict[str, Any]:
    """把一条 v3 Decision / Finding 迁移为 v4 Decision。无法迁移时抛 MigrationRefused。"""
    for key in REJECTED_KEYS:
        if key in v3:
            raise MigrationRefused(f"含明文引用字段 {key!r}，拒绝迁移；请重新检测生成 v4 记录")

    finding_id = v3.get("finding_id") or v3.get("id")
    if not finding_id:
        raise MigrationRefused("缺少 finding_id / id，无法建立引用")

    if "selected" not in v3 or "action" not in v3:
        raise MigrationRefused("缺少 selected 或 action，不构成可迁移的 v3 决定")

    selected = v3["selected"]
    action = v3["action"]
    if not isinstance(selected, bool):
        raise MigrationRefused(f"selected 必须是布尔值，实际为 {selected!r}")

    mapped = _ALLOWED.get((selected, action))
    if mapped is None:
        raise MigrationRefused(
            f"selected={selected} + action={action!r} 是歧义组合，契约 v4 不接受"
        )

    revision = v3.get("revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        revision = 1

    return {
        "contract_version": CONTRACT_VERSION,
        "record_type": "Decision",
        "finding_id": str(finding_id),
        "action": mapped,
        "revision": revision,
    }


def migrate_records(
    records: Iterable[Mapping[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """批量迁移。返回 (迁移结果, 拒绝清单)；拒绝清单里写明 id 与原因。"""
    migrated: List[Dict[str, Any]] = []
    refused: List[Dict[str, str]] = []
    for idx, rec in enumerate(records):
        ident = str(rec.get("id") or rec.get("finding_id") or f"#{idx}")
        try:
            migrated.append(migrate_decision(rec))
        except MigrationRefused as exc:
            refused.append({"id": ident, "reason": str(exc)})
    return migrated, refused


REVERIFY_NOTE = "旧 verify_status 已丢弃且不受信任；迁移出的计划必须对当前 v4 计划重新复检"
