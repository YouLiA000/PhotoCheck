#!/usr/bin/env python3
"""标注 CSV <-> 契约 v4 记录的双向转换（C · R-01 / K-13）。

字段以《功能规格卡 v1.4》K-13「标注字段」表为准：

    file / set / scene_id | kind / subtype / bbox | should_hide | difficulty | note

其中 kind/subtype/bbox 是**客观真值**，should_hide 是**策略标签**，两者分开。
`risk` 不在标注字段里 —— 它由 `contract/risk_rules.py` 按 K-04/K-10 规则算出，
标注者不判风险，避免同一条 Finding 在 A 和 C 手里出现两个分值。

CSV 分三段：
  规格卡字段   file set scene_id kind subtype bbox should_hide difficulty note
  C 的扩展     verdict det_id masked_preview idcard_check decodable
  流程留痕     annotator checked
扩展列只服务于"把真值喂进契约 v4"，不进契约记录。

用法：
  python dataset/tools/labels_csv.py export            # JSON 真值 → labels.csv
  python dataset/tools/labels_csv.py import            # labels.csv → 真值并校验
  python dataset/tools/labels_csv.py import --write-back
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from contract.risk_rules import detection_risk  # noqa: E402
from contract.validate_records import validate_records  # noqa: E402

CONTRACT_VERSION = "photocheck/v4"
EVIDENCE = {"meta": "exif", "text": "ocr_rule", "qr": "qr_payload", "face": "face_box"}

# 规格卡 K-13 的顺序，一个字段不改
SPEC_COLUMNS = ["file", "set", "scene_id", "kind", "subtype", "bbox",
                "should_hide", "difficulty", "note"]
EXT_COLUMNS = ["verdict", "det_id", "masked_preview", "idcard_check", "decodable",
               "safe_domain", "safe_ssid"]
FLOW_COLUMNS = ["annotator", "checked"]
COLUMNS = SPEC_COLUMNS + EXT_COLUMNS + FLOW_COLUMNS

_BBOX_SPLIT = re.compile(r"[\s,;]+")


def _parse_bbox(text: str) -> List[int]:
    parts = [p for p in _BBOX_SPLIT.split(str(text).strip()) if p]
    if len(parts) != 4:
        raise ValueError(f"bbox 要求 x,y,w,h 四段，实际 {text!r}")
    try:
        values = [Decimal(part) for part in parts]
    except InvalidOperation as exc:
        raise ValueError(f"bbox 必须是整数坐标，实际 {text!r}") from exc
    if any(not value.is_finite() or value != value.to_integral_value() for value in values):
        raise ValueError(f"bbox 必须是整数坐标，实际 {text!r}")
    return [int(value) for value in values]


def _fmt_bbox(bbox: Sequence[int]) -> str:
    return ",".join(str(int(v)) for v in bbox)


def _image_id_of(file_field: str) -> str:
    return Path(str(file_field).replace("\\", "/")).stem


def export_rows(
    records_by_image: Mapping[str, Sequence[Mapping]],
    negatives_by_image: Mapping[str, Sequence[Mapping]],
    meta_by_image: Optional[Mapping[str, Mapping]] = None,
) -> List[Dict[str, str]]:
    """真值 → CSV 行。先 positive（按契约顺序），再 negative。"""
    meta_by_image = meta_by_image or {}

    def header(image_id: str, verdict: str) -> Dict[str, str]:
        info = meta_by_image.get(image_id, {})
        return {
            "file": f"images/{image_id}.jpg",
            "set": str(info.get("set", "")),
            "scene_id": str(info.get("scene_id", "")),
            "kind": "",
            "subtype": "",
            "bbox": "",
            "should_hide": "0" if verdict in ("negative", "clean") else "1",
            "difficulty": str(info.get("difficulty", "")),
            "note": "",
            "verdict": verdict,
            "det_id": "",
            "masked_preview": "",
            "idcard_check": "",
            "decodable": "",
            "safe_domain": "",
            "safe_ssid": "",
            "annotator": "",
            "checked": "0",
        }

    rows: List[Dict[str, str]] = []
    for image_id, records in records_by_image.items():
        emitted = 0
        for rec in records:
            if rec.get("record_type") != "Detection":
                continue
            details = rec.get("details") or {}
            row = header(image_id, "positive")
            row.update(
                {
                    "kind": rec["kind"],
                    "subtype": rec["subtype"],
                    "bbox": "" if rec.get("bbox") is None else _fmt_bbox(rec["bbox"]),
                    "det_id": rec["id"],
                    "masked_preview": details.get("masked_preview", ""),
                    "idcard_check": details.get("idcard_check", ""),
                    "decodable": "" if "decodable" not in details else str(int(bool(details["decodable"]))),
                    "safe_domain": details.get("safe_domain", ""),
                    "safe_ssid": details.get("safe_ssid", ""),
                }
            )
            rows.append(row)
            emitted += 1

        for item in negatives_by_image.get(image_id, []):
            row = header(image_id, "negative")
            row.update(
                {
                    "bbox": _fmt_bbox(item["bbox"]),
                    "note": item.get("note", ""),
                }
            )
            rows.append(row)
            emitted += 1

        if emitted == 0:
            # 干净负样本（整图没有敏感对象）原本在 CSV 里**一行都没有**，
            # 于是导出一看只有 29 张图、真值却有 35 张，6 张干净图在台账里等于不存在；
            # 谁按 CSV 清理一遍，就会把它们当孤儿删掉。这里补一行显式标记。
            row = header(image_id, "clean")
            row["note"] = "整图无敏感对象（干净负样本，用于误报率）"
            rows.append(row)
    return rows


def import_rows(
    rows: Iterable[Mapping[str, str]],
    sizes: Mapping[str, Tuple[int, int]],
    *,
    orientation: str = "exif_transposed",
) -> Tuple[Dict[str, List[dict]], Dict[str, List[dict]]]:
    """CSV 行 → 真值。缺尺寸的图直接拒绝，不猜。

    负样本两种写法都接受：`verdict=negative`，或 kind/subtype 留空。
    规格卡字段表里没有 verdict 这一列，只用规格卡字段的人会写成空 kind。
    """
    positives: Dict[str, List[dict]] = {}
    negatives: Dict[str, List[dict]] = {}
    cleans: set[str] = set()

    for lineno, row in enumerate(rows, start=2):  # 第 1 行是表头
        file_field = (row.get("file") or "").strip()
        if not file_field:
            raise ValueError(f"第 {lineno} 行：file 为空")
        image_id = _image_id_of(file_field)

        kind = (row.get("kind") or "").strip()
        subtype = (row.get("subtype") or "").strip()
        verdict = (row.get("verdict") or "").strip().lower()
        if verdict not in ("", "positive", "negative", "clean"):
            raise ValueError(
                f"第 {lineno} 行：verdict 只能是 positive / negative / clean，实际 {verdict!r}"
            )
        if verdict == "clean":
            # 干净负样本：整图没有敏感对象。要保留 image_id，否则这张图会从真值里消失
            if kind or subtype:
                raise ValueError(f"第 {lineno} 行：verdict=clean 的行不该写 kind/subtype")
            cleans.add(image_id)
            continue

        if verdict == "negative" or (verdict == "" and not kind and not subtype):
            negatives.setdefault(image_id, []).append(
                {"bbox": _parse_bbox(row.get("bbox", "")), "note": (row.get("note") or "").strip()}
            )
            continue

        if kind not in EVIDENCE:
            raise ValueError(f"第 {lineno} 行：kind 必须是 {sorted(EVIDENCE)}，实际 {kind!r}")
        if not subtype:
            raise ValueError(f"第 {lineno} 行：positive 行必须写 subtype")

        details: Dict[str, object] = {}
        if kind == "text":
            preview = (row.get("masked_preview") or "").strip()
            if not preview:
                raise ValueError(f"第 {lineno} 行：文字类必须给 masked_preview（打码，不是明文）")
            if "*" not in preview:
                raise ValueError(f"第 {lineno} 行：masked_preview 必须含 * 掩码，禁止写明文号码")
            details["masked_preview"] = preview
            tri = (row.get("idcard_check") or "").strip()
            if subtype == "idcard":
                if tri not in ("pass", "fail", "na"):
                    raise ValueError(f"第 {lineno} 行：身份证行必须给 idcard_check = pass/fail/na")
                details["idcard_check"] = tri
            elif tri:
                details["idcard_check"] = tri
        elif kind == "meta":
            preview = (row.get("masked_preview") or "").strip()
            if preview:
                details["masked_preview"] = preview
        elif kind == "qr":
            flag = (row.get("decodable") or "").strip()
            if flag not in ("0", "1"):
                raise ValueError(f"第 {lineno} 行：二维码行必须给 decodable = 0/1")
            details["decodable"] = flag == "1"
            domain = (row.get("safe_domain") or "").strip()
            ssid = (row.get("safe_ssid") or "").strip()
            if domain:
                if subtype != "qr_link":
                    raise ValueError(f"第 {lineno} 行：safe_domain 只用于 qr_link，当前 subtype={subtype}")
                details["safe_domain"] = domain
            if ssid:
                if subtype != "qr_wifi":
                    raise ValueError(f"第 {lineno} 行：safe_ssid 只用于 qr_wifi，当前 subtype={subtype}")
                details["safe_ssid"] = ssid

        det_id = (row.get("det_id") or "").strip()
        if not det_id:
            if kind == "meta":
                det_id = f"det-{image_id}-meta-{subtype}"
            else:
                same_kind = sum(1 for r in positives.get(image_id, []) if r["kind"] == kind)
                det_id = f"det-{image_id}-{kind}-{same_kind + 1:02d}"

        record = {
            "contract_version": CONTRACT_VERSION,
            "record_type": "Detection",
            "id": det_id,
            "image_id": image_id,
            "kind": kind,
            "subtype": subtype,
            "bbox": None if kind == "meta" else _parse_bbox(row.get("bbox", "")),
            "detector_score": None,
            "evidence": EVIDENCE[kind],
            # risk 不来自 CSV：按 K-04 / K-10 规则算，与 A 共用同一份实现
            "risk": detection_risk(kind, subtype, details),
            "details": details,
        }
        positives.setdefault(image_id, []).append(record)

    out: Dict[str, List[dict]] = {}
    conflicting = cleans & (set(positives) | set(negatives))
    if conflicting:
        names = "、".join(sorted(conflicting)[:3])
        raise ValueError(f"{names} 同时标为 clean 且有对象/负例行，一张图只能取一种状态")
    for image_id in set(positives) | set(negatives) | cleans:
        if image_id not in sizes:
            raise ValueError(f"{image_id}: 缺少图像尺寸，无法构造 ImageDescriptor（先跑 manifest）")
        width, height = sizes[image_id]
        negatives.setdefault(image_id, [])
        out[image_id] = [
            {
                "contract_version": CONTRACT_VERSION,
                "record_type": "ImageDescriptor",
                "image_id": image_id,
                "width": width,
                "height": height,
                "orientation": orientation,
                "coordinate_space": "top_left_pixels",
            }
        ] + positives.get(image_id, [])
    return out, negatives


def load_sizes(dataset_dir: Path) -> Dict[str, Tuple[int, int]]:
    manifest = dataset_dir / "manifest.csv"
    if manifest.exists():
        with open(manifest, encoding="utf-8-sig", newline="") as fh:
            return {
                row["image_id"]: (int(row["width"]), int(row["height"]))
                for row in csv.DictReader(fh)
            }
    from PIL import Image  # 只有回退路径需要 Pillow

    sizes = {}
    for path in (dataset_dir / "images").glob("*.jpg"):
        with Image.open(path) as im:
            sizes[path.stem] = im.size
    return sizes


def load_manifest_meta(dataset_dir: Path) -> Dict[str, Dict[str, str]]:
    """从 manifest.csv 取 set / scene_id / difficulty，供导出时填台账列。"""
    manifest = dataset_dir / "manifest.csv"
    if not manifest.exists():
        return {}
    with open(manifest, encoding="utf-8-sig", newline="") as fh:
        return {
            row["image_id"]: {
                "set": row.get("set", ""),
                "scene_id": row.get("scene_id", ""),
                "difficulty": row.get("difficulty", ""),
            }
            for row in csv.DictReader(fh)
        }


def export_dir(dataset_dir: Path) -> Path:
    labels = dataset_dir / "labels"
    records_by_image = {
        p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(labels.glob("*.json"))
    }
    negative_path = dataset_dir / "negatives.json"
    negatives = json.loads(negative_path.read_text(encoding="utf-8")) if negative_path.exists() else {}
    rows = export_rows(records_by_image, negatives, load_manifest_meta(dataset_dir))

    out = dataset_dir / "labels.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return out


def import_dir(dataset_dir: Path, *, write_back: bool = False) -> int:
    with open(dataset_dir / "labels.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    sizes = load_sizes(dataset_dir)
    records_by_image, negatives = import_rows(rows, sizes)
    # 没有负例的图也保留空列表：往返才能逐字节还原，也免得每次写回都改一遍文件
    negatives = {k: negatives.get(k, []) for k in sorted(set(sizes) | set(negatives))}

    total_errors = 0
    for image_id, records in records_by_image.items():
        report = validate_records(records)
        if not report.ok:
            total_errors += len(report.errors)
            print(f"{image_id}: {len(report.errors)} 个错误")
            for issue in report.errors[:5]:
                print("   " + str(issue))

    if write_back and not total_errors:
        for image_id, records in records_by_image.items():
            (dataset_dir / "labels" / f"{image_id}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8", newline="\n",
            )
        (dataset_dir / "negatives.json").write_text(
            json.dumps(negatives, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n",
        )

    print(f"CSV {len(rows)} 行 → {len(records_by_image)} 张图，"
          f"{sum(len(v) for v in negatives.values())} 条负例，{total_errors} 个错误")
    return 1 if total_errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="标注 CSV <-> 契约 v4 记录")
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("--dir", default=str(ROOT / "dataset" / "dev"))
    ap.add_argument("--write-back", action="store_true", help="import 时把结果写回 JSON 真值")
    args = ap.parse_args(argv)

    dataset_dir = Path(args.dir)
    if args.action == "export":
        out = export_dir(dataset_dir)
        print(f"已导出 {out}")
        return 0
    return import_dir(dataset_dir, write_back=args.write_back)


if __name__ == "__main__":
    sys.exit(main())
