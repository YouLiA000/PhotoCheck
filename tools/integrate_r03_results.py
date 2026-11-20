#!/usr/bin/env python3
"""Merge A's real layer-2 export results and recorded layer-3 review into eval output."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import reporter  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_layer1(results: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(
        (path for path in results.iterdir() if path.is_dir() and (path / "summary.json").is_file()),
        key=lambda path: (path / "summary.json").stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        data = _load(path / "summary.json")
        if (data.get("layer1") or {}).get("objects_truth"):
            return path, data
    raise FileNotFoundError("no existing report has valid layer1 data")


def _layer2(path: Path) -> dict[str, Any]:
    data = _load(path)
    rows = data.get("rows") or []
    residual_count = sum(int(row.get("residual_count") or 0) for row in rows)
    metadata_count = sum(len(row.get("metadata_tags") or []) for row in rows)
    clean = sum(
        int(row.get("residual_count") or 0) == 0
        and len(row.get("metadata_tags") or []) == 0
        for row in rows
    )
    state_counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("disposal_state") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "status": "passed" if rows and clean == len(rows) else "failed",
        "reason": "对最终导出 JPG 重新检测并独立扫描元数据；中间图不参与该数字",
        "images": len(rows),
        "clean": clean,
        "residuals": residual_count,
        "metadata_tags": metadata_count,
        "disposal_state_counts": state_counts,
        "items": [
            {"检查项目": "文字 / 二维码导出残留", "通过": residual_count == 0,
             "失败": residual_count, "状态": "passed" if residual_count == 0 else "failed",
             "说明": "在最终导出文件上重跑检测"},
            {"检查项目": "导出文件存活元数据", "通过": metadata_count == 0,
             "失败": metadata_count, "状态": "passed" if metadata_count == 0 else "failed",
             "说明": "EXIF/XMP/IPTC/PNG 文本块"},
            {"检查项目": "导出文件数", "通过": len(rows), "失败": 0,
             "状态": "passed", "说明": f"导出干净 {clean}/{len(rows)}"},
            {"检查项目": "处置状态分布", "通过": sum(state_counts.values()), "失败": 0,
             "状态": "passed", "说明": json.dumps(state_counts, ensure_ascii=False)},
        ],
    }


def _layer3(path: Path) -> dict[str, Any]:
    data = _load(path)
    reviewed = int(data["reviewed_images"])
    appropriate = int(data["appropriate"])
    over = int(data["over_redacted"])
    under = int(data["under_redacted"])
    status = "passed" if reviewed and appropriate == reviewed and over == under == 0 else "failed"
    return {
        **data,
        "status": status,
        "reason": data.get("decision", "人工逐图判读"),
        "items": [
            {"项目": "误遮对象数", "数值": over, "状态": status, "说明": "人工判读"},
            {"项目": "欠遮对象数（本口径）", "数值": under, "状态": status, "说明": "人工判读"},
            {"项目": "判读一致数", "数值": int(data["agreed"]), "状态": status,
             "说明": f"{data['reviewers']} 名判读人"},
        ],
    }


def integrate(
    *,
    layer2_path: Path,
    layer3_path: Path,
    results: Path,
    out: Path,
) -> Path:
    _, previous = _latest_layer1(results)
    summary = previous["layer1"]
    context = dict(previous.get("context") or {})
    context.update(
        {
            "dataset": "dev",
            "prediction": "A layer2 export audit + recorded R-03 review",
            "generated_at": "2026-11-15",
            "layer2_source": layer2_path.relative_to(ROOT).as_posix(),
            "layer3_source": layer3_path.relative_to(ROOT).as_posix(),
        }
    )
    layer2 = _layer2(layer2_path)
    layer3 = _layer3(layer3_path)
    out.mkdir(parents=True, exist_ok=True)
    reporter.write_reports(out, summary, layer2, layer3, context, failures=[])
    shutil.copy2(layer3_path, out / "layer3_review.json")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Integrate layer2/layer3 evidence")
    parser.add_argument(
        "--layer2",
        default=str(ROOT / "photocheck" / "detectors" / "a_engine" / "reports" / "layer2_export_20261114.json"),
    )
    parser.add_argument(
        "--layer3",
        default=str(ROOT / "docs" / "R03人工判读_v1.4五类范围_2026-11-15.json"),
    )
    parser.add_argument("--results", default=str(ROOT / "results"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "r03-layer23-20261115"))
    args = parser.parse_args(argv)
    out = integrate(
        layer2_path=Path(args.layer2),
        layer3_path=Path(args.layer3),
        results=Path(args.results),
        out=Path(args.out),
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
