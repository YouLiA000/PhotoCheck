#!/usr/bin/env python3
"""第二层评测：导出文件残留。

对 dev（或指定目录）的每张图跑完整链路，然后**用 ExifTool 独立复验导出文件**——
判定不依赖被测实现自己的 `disposal_state`。

## 为什么必须独立复验

如果只看 `process()` 返回的 `passed`，测的是"它以为自己清干净了"。
必须拿最终文件去读，才知道真的干净。

## 一个必须避开的误报

标准 sRGB ICC profile 自带 `Device Model` / `ProfileDateTime` 等字段。
用"字段名含 Model 就算残留"这种宽判据会把 **38/38 全判成有残留**（2026-11-14 实测踩过）。
正确做法：只查 `-EXIF:all -XMP:all -IPTC:all -MakerNotes:all`，排除 ICC / JFIF / File / System。

## 用法

    # 只跑第二层
    python tools/run_layer2.py --pkg D:\\some\\PhotoCheck_App

    # 同时导出第一层预测，供 eval 复用同一批检测结果
    python tools/run_layer2.py --pkg ... --predictions D:\\tmp\\pred \\
        --layer2-json D:\\tmp\\layer2.json

`--pkg` 指向解压出来的 `PhotoCheck_App` 目录（里面有 `app/`）。
"""
from __future__ import annotations

import argparse
import collections
import copy
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META_ARGS = ["-EXIF:all", "-XMP:all", "-IPTC:all", "-MakerNotes:all"]


def read_private_tags(exiftool: Path, image: Path) -> list[str]:
    """只读会泄露隐私的组。ICC / JFIF / File / System 一律不算。"""
    try:
        out = subprocess.run(
            [str(exiftool), "-j", *META_ARGS, str(image)],
            capture_output=True, text=True, timeout=45, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return ["__exiftool_failed__"]
        data = json.loads(out.stdout) or [{}]
        tags = data[0]
        tags.pop("SourceFile", None)
        return sorted(tags)
    except Exception as exc:  # noqa: BLE001
        return [f"__read_error__:{type(exc).__name__}"]


def write_predictions(pred_dir: Path, image_id: str, records: list[dict]) -> None:
    """一图一文件，image_id 必须与文件名一致（eval/loader 会强校验）。

    `service.detect` 内部的 image_id 是上传副本的哈希（`image-<hex>`），
    直接落盘会让评测按 `image-dev-001` 对不上、整批预测落空。
    """
    pred_dir.mkdir(parents=True, exist_ok=True)
    # 预测文件会进入评测产物。落盘前先过契约校验，避免检测器回归把原文塞进
    # `masked_preview`、安全摘要或未知字段，再由本工具把它持久化。
    from photocheck.contract.validation import validate_record

    fixed = []
    for rec in records:
        row = copy.deepcopy(rec)
        row["image_id"] = image_id
        validate_record(row)
        fixed.append(row)
    (pred_dir / f"{image_id}.json").write_text(
        json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def plan_regions_for_evidence(regions: list[dict], detections: list[dict]) -> list[dict]:
    """复制计划区域并补 subtype；这是评测证据，不含文字或 payload。"""
    subtype_by_id = {d["id"]: d.get("subtype") for d in detections}
    out = []
    for region in regions:
        finding_id = region.get("finding_id")
        out.append({
            "id": region["id"],
            "finding_id": finding_id,
            "bbox": list(region["bbox"]),
            "method": region["method"],
            "subtype": subtype_by_id.get(finding_id) if finding_id else "manual",
        })
    return out


def summarize(rows: list[dict]) -> dict:
    """把逐图结果压成可判定的汇总。空表不许标 ok。"""
    checks = sum(r.get("checks") or 0 for r in rows)
    not_passed = sum(r.get("check_not_passed") or 0 for r in rows)
    summary = {
        "images": len(rows),
        "images_with_export": sum(1 for r in rows if r.get("out")),
        "images_not_applicable": sum(1 for r in rows if r.get("state") == "not_applicable"),
        "regions": sum(r.get("plan") or 0 for r in rows),
        "verification_checks": checks,
        "verification_passed": checks - not_passed,
        "verification_failed": sum(r.get("check_fail") or 0 for r in rows),
        "residuals": sum(r.get("residuals") or 0 for r in rows),
        "export_metadata_checked": sum(1 for r in rows if r.get("export_meta_count") is not None),
        "export_metadata_residual": sum(1 for r in rows if (r.get("export_meta_count") or 0) > 0),
        "errors": sum(1 for r in rows if r.get("error")),
    }
    ms = [r["ms"] for r in rows if r.get("ms")]
    if ms:
        summary["median_ms"] = round(statistics.median(ms))
        summary["max_ms"] = round(max(ms))
    return summary


def build_layer2_result(rows: list[dict], method: str) -> dict:
    """生成第二层结论；只有每张图都导出并完成元数据复验才能是 ok。"""
    summary = summarize(rows)
    missing_plan_evidence = sum(
        1
        for row in rows
        if not isinstance(row.get("plan_regions"), list)
        or len(row["plan_regions"]) != int(row.get("plan") or 0)
    )
    complete = (
        summary["images"] > 0
        and summary["images_with_export"] == summary["images"]
        and summary["export_metadata_checked"] == summary["images"]
        and missing_plan_evidence == 0
    )
    ok = summary["errors"] == 0 and complete
    if ok:
        reason = ""
    else:
        missing_export = summary["images"] - summary["images_with_export"]
        missing_meta = summary["images"] - summary["export_metadata_checked"]
        reason = (
            f"{summary['errors']} 张出错；缺导出 {missing_export} 张；"
            f"未完成元数据检查 {missing_meta} 张；计划区域证据不全 "
            f"{missing_plan_evidence} 张"
        )
    return {
        "status": "ok" if ok else "failed",
        "reason": reason,
        "method": method,
        "summary": summary,
        "items": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="第二层：导出文件残留评测")
    ap.add_argument("--pkg", required=True, help="解压后的 PhotoCheck_App 目录")
    ap.add_argument("--images", default=str(ROOT / "dataset/dev/images"), help="待测图片目录")
    ap.add_argument("--manifest", default=str(ROOT / "dataset/dev/manifest.csv"),
                    help="manifest，用来排除 broken 图（可选）")
    ap.add_argument("--method", default="solid", choices=["solid", "blur", "mosaic"])
    ap.add_argument("--out", default=str(ROOT / "results/layer2"), help="结果目录")
    ap.add_argument("--predictions", default="", help="把每图检测写成契约记录到该目录")
    ap.add_argument("--layer2-json", default="", help="第二层汇总 JSON；默认 <out>/layer2.json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    pkg = Path(args.pkg).resolve()
    sys.path.insert(0, str(pkg))
    sys.path.insert(0, str(pkg / "app" / "vendor"))
    exiftool = Path(os.environ.get("PHOTOCHECK_EXIFTOOL") or (ROOT / "tools/exiftool/exiftool.exe"))
    if not exiftool.is_file():
        raise SystemExit(f"找不到 ExifTool：{exiftool}")
    os.environ["PHOTOCHECK_EXIFTOOL"] = str(exiftool)

    from app import service  # noqa: E402 - 必须在 sys.path 之后

    images = sorted(Path(args.images).glob("*.jpg"))
    skip: set[str] = set()
    mf = Path(args.manifest)
    if mf.is_file():
        for row in csv.DictReader(mf.open(encoding="utf-8-sig")):
            if row.get("template") == "broken":
                skip.add(row["image_id"])
    images = [p for p in images if p.stem not in skip]
    if args.limit:
        images = images[: args.limit]
    print(f"待测 {len(images)} 张，遮盖方式 {args.method}，跳过 broken {len(skip)} 张")

    pred_dir = Path(args.predictions) if args.predictions else None
    rows: list[dict] = []
    t_all = time.time()
    for i, img in enumerate(images, 1):
        rec: dict = {"image_id": img.stem}
        t0 = time.perf_counter()
        try:
            sid = service.save_upload(img.name, img.read_bytes())
            det = service.detect(sid)
            ids = [d["id"] for d in det["detections"] if d.get("kind") != "meta"]
            rec.update(det=len(det["detections"]), actionable=len(ids))

            if pred_dir is not None:
                # SESSION 里存的是契约记录；UI 返回的是摊平后的视图。
                # 第一层要的是契约记录，所以从 SESSION 取，不从返回值反推。
                analysis = service.SESSION.get(sid)
                write_predictions(pred_dir, img.stem, list(analysis.detections))

            rep = service.process(sid, ids, args.method)
            checks = rep.get("checks") or []
            plan_regions = plan_regions_for_evidence(
                rep.get("plan_regions") or [],
                list(service.SESSION.get(sid).detections),
            )
            rec.update(
                state=rep.get("disposal_state"),
                plan=rep.get("plan_items"),
                plan_regions=plan_regions,
                checks=len(checks),
                check_fail=sum(1 for c in checks if c.get("status") == "failed"),
                check_not_passed=sum(1 for c in checks if c.get("status") != "passed"),
                residuals=len(rep.get("residuals") or []),
                out=rep.get("output_path"),
            )
            out = Path(rec["out"]) if rec["out"] else None
            if out and out.is_file():
                tags = read_private_tags(exiftool, out)
                rec["export_meta_count"] = len(tags)
                rec["export_meta_tags"] = tags
        except Exception as exc:  # noqa: BLE001
            # 异常正文可能包含 OCR 原文、识别 payload 或原图路径。评测产物只记录
            # 异常类型；详细文本留在进程内调试，不能写入可序列化的 JSON / CSV。
            rec["error"] = type(exc).__name__
        rec["ms"] = round((time.perf_counter() - t0) * 1000)
        rows.append(rec)
        print(f"[{i:2}/{len(images)}] {img.stem:18} {str(rec.get('state') or rec.get('error'))[:20]:20}"
              f" 计划{rec.get('plan', '-')} 元数据{rec.get('export_meta_count', '-')}", flush=True)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    cols = ["image_id", "state", "det", "actionable", "plan", "checks", "check_fail",
            "check_not_passed", "residuals", "ms", "export_meta_count"]
    with (outdir / "dev_layer2.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols + ["export_meta_tags"])
        for r in rows:
            w.writerow([r.get(c, "") for c in cols] + [";".join(r.get("export_meta_tags") or [])])
    (outdir / "dev_layer2.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")

    layer2 = build_layer2_result(rows, args.method)
    summary = layer2["summary"]
    ok = layer2["status"] == "ok"
    layer2_path = Path(args.layer2_json) if args.layer2_json else outdir / "layer2.json"
    layer2_path.write_text(
        json.dumps(layer2, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")

    dirty = [r["image_id"] for r in rows if r.get("export_meta_count")]
    print("\n=== 汇总 ===")
    print("  状态:", dict(collections.Counter(r.get("state") or "error" for r in rows)))
    print("  计划区域:", summary["regions"],
          " 复检项:", summary["verification_checks"],
          " 复检失败:", summary["verification_failed"])
    print("  残留项:", summary["residuals"])
    print(f"  导出文件元数据残留: {summary['export_metadata_residual']} 个 {dirty[:5]}")
    if summary.get("median_ms"):
        print(f"  单张中位 {summary['median_ms']/1000:.1f}s  最慢 {summary['max_ms']/1000:.1f}s")
    print(f"  layer2 JSON → {layer2_path}")
    if pred_dir is not None:
        print(f"  预测目录 → {pred_dir}（{len(list(pred_dir.glob('*.json')))} 个文件）")
    print(f"  用时 {(time.time()-t_all)/60:.1f} 分钟 → {outdir}")
    return 0 if ok and not dirty else 1


if __name__ == "__main__":
    raise SystemExit(main())
