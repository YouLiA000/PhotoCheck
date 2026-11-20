#!/usr/bin/env python3
"""第三层评测的**人工判读表**：把每个遮盖区域的前后对照裁出来，等人填。

为什么必须人工
--------------
"是否影响阅读" 不是像素问题。同一块黑色，盖住的是二维码，还是盖住了旁边的地址、
金额、条形码，机器判不出来。自动代理指标（遮盖面积占比）只能提示"盖得多"，
不能替人回答"盖得对不对"。

这个脚本只做三件机器能做的事：
1. 按 bbox 从**原图**和**导出图**裁同一块区域（带外扩，看得到周边被牵连的内容）；
2. 拼成左右对照图，编上行号；
3. 生成 CSV 模板。**空着就是空着，不预填任何判定。**

填完之后
--------
    python tools/make_layer3_sheet.py --from-csv <review.csv> \\
        --layer2 <layer2.json> --out-json layer3.json

`--from-csv` 会检查每一行都填了 0/1，并把 CSV 的区域与第二层 RedactionPlan 逐项对齐；
缺行、多行或 bbox 被改动都会返回非零，不给"部分填写也算通过"。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = [
    "row", "image_id", "finding_id", "subtype", "bbox",
    "affects_reading", "over_redacted", "note",
]


def expected_regions(layer2: dict) -> dict[tuple[str, str], dict]:
    """从第二层结果取真正的 RedactionPlan 区域。

    不能用 Detection 反推：计划可能合并相邻框、外扩、加入手动框，也可能保留
    某些检测。第三层审的是最终计划，漏掉手动框会让过度脱敏证据不完整。
    """
    expected: dict[tuple[str, str], dict] = {}
    for item in layer2.get("items") or []:
        image_id = item.get("image_id")
        regions = item.get("plan_regions")
        if not image_id or not item.get("out"):
            continue
        if not isinstance(regions, list):
            raise ValueError(
                f"{image_id} 的 layer2 结果没有 plan_regions；请用新版 tools/run_layer2.py 重跑"
            )
        for region in regions:
            region_id = str(region.get("finding_id") or region.get("id") or "")
            if not region_id or not region.get("bbox"):
                raise ValueError(f"{image_id} 的 RedactionPlan 区域缺少 id/bbox")
            key = (image_id, region_id)
            if key in expected:
                raise ValueError(f"RedactionPlan 区域重复：{key}")
            expected[key] = region
    return expected


def expand(bbox: list[int], width: int, height: int, ratio: float = 0.25) -> list[int]:
    x, y, w, h = bbox
    pad = max(12, int(max(w, h) * ratio))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
    return [x0, y0, x1 - x0, y1 - y0]


def side_by_side(a: Image.Image, b: Image.Image, width: int = 320) -> Image.Image:
    def fit(im: Image.Image) -> Image.Image:
        ratio = width / im.width
        return im.resize((width, max(1, round(im.height * ratio))), Image.LANCZOS)

    a, b = fit(a), fit(b)
    h = max(a.height, b.height)
    canvas = Image.new("RGB", (width * 2 + 8, h), (210, 213, 218))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (width + 8, 0))
    return canvas


def build_sheet(args: argparse.Namespace) -> int:
    layer2 = json.loads(Path(args.layer2).read_text(encoding="utf-8"))
    if layer2.get("status") != "ok":
        print(f"第二层不是 ok（{layer2.get('status')}），先把它跑绿", file=sys.stderr)
        return 2

    images_dir = Path(args.images)
    out_dir = Path(args.out)
    crop_dir = out_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    html: list[str] = []
    for item in layer2.get("items") or []:
        image_id = item.get("image_id")
        out_path = item.get("out")
        if not image_id or not out_path:
            continue
        src_path = images_dir / f"{image_id}.jpg"
        if not src_path.is_file() or not Path(out_path).is_file():
            print(f"[skip] {image_id}: 缺原图 / 导出图", file=sys.stderr)
            continue

        src = Image.open(src_path).convert("RGB")
        dst = Image.open(out_path).convert("RGB")
        if src.size != dst.size:
            print(f"[skip] {image_id}: 原图与导出图尺寸不同 {src.size} vs {dst.size}", file=sys.stderr)
            continue

        regions = item.get("plan_regions")
        if not isinstance(regions, list):
            print(f"[skip] {image_id}: 缺 plan_regions，请用新版 run_layer2 重跑", file=sys.stderr)
            continue
        for region in regions:
            bbox = list(region["bbox"])
            box = expand(bbox, src.width, src.height)
            x, y, w, h = box
            crop = side_by_side(src.crop((x, y, x + w, y + h)),
                                dst.crop((x, y, x + w, y + h)))
            row = len(rows) + 1
            finding_id = str(region.get("finding_id") or region["id"])
            name = f"{row:03d}_{image_id}_{finding_id.replace(':', '_')}.png"
            crop.save(crop_dir / name)
            rows.append({
                "row": row, "image_id": image_id, "finding_id": finding_id,
                "subtype": region.get("subtype") or (
                    "manual" if region.get("finding_id") is None else "unknown"
                ),
                "bbox": ",".join(str(v) for v in bbox),
                "affects_reading": "", "over_redacted": "", "note": "",
            })
            html.append(
                f'<tr><td>{row}</td><td>{image_id}</td><td>{finding_id}</td>'
                f'<td>{region.get("subtype") or "manual"}</td><td>{bbox}</td>'
                f'<td><img src="crops/{name}" loading="lazy"></td></tr>'
            )

    if not rows:
        print("没有可判读的区域：检查 layer2 是否为包含 plan_regions 的新版结果",
              file=sys.stderr)
        return 2

    with (out_dir / "layer3_review.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    page = f"""<!doctype html>
<meta charset="utf-8">
<title>第三层人工判读 · {len(rows)} 个区域</title>
<style>
 body {{ font: 14px/1.6 system-ui, "Microsoft YaHei", sans-serif; margin: 24px; color: #1b1f24; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border-bottom: 1px solid #e3e6ea; padding: 8px; vertical-align: top; text-align: left; }}
 th {{ position: sticky; top: 0; background: #fff; }}
 img {{ width: 640px; border: 1px solid #e3e6ea; border-radius: 6px; }}
 .hint {{ background: #f6f8fa; padding: 12px 14px; border-radius: 8px; margin-bottom: 16px; }}
 code {{ background: #eef1f4; padding: 1px 4px; border-radius: 3px; }}
</style>
<div class="hint">
 <b>左 = 原图，右 = 导出图</b>（同一块区域，外扩 25% 以便看到周边被牵连的内容）。<br>
 判读两列并填 CSV：<code>affects_reading</code>（1 = 盖住了非敏感但需要读的内容）、
 <code>over_redacted</code>（1 = 明显超出敏感对象本身）。<br>
 每一行都要填，缺行时 <code>--from-csv</code> 会拒绝生成。
</div>
<table>
<thead><tr><th>#</th><th>image_id</th><th>finding</th><th>subtype</th><th>bbox</th><th>对照</th></tr></thead>
<tbody>
{''.join(html)}
</tbody></table>
"""
    (out_dir / "layer3_sheet.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"已生成 {len(rows)} 个区域：")
    print(f"  对照页 {out_dir / 'layer3_sheet.html'}")
    print(f"  填写表 {out_dir / 'layer3_review.csv'}")
    print("填完后：python tools/make_layer3_sheet.py --from-csv <csv> "
          "--layer2 <layer2.json> --out-json <json>")
    return 0


def from_csv(args: argparse.Namespace) -> int:
    layer2_path = Path(getattr(args, "layer2", "") or "")
    if not layer2_path.is_file():
        print("--from-csv 必须同时提供 --layer2，否则无法证明判读表没有漏行", file=sys.stderr)
        return 2

    layer2 = json.loads(layer2_path.read_text(encoding="utf-8"))
    if layer2.get("status") != "ok":
        print(f"第二层不是 ok（{layer2.get('status')}），不能生成第三层结论", file=sys.stderr)
        return 2

    try:
        expected = expected_regions(layer2)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    if not expected:
        print("[error] 来源中没有可判读区域", file=sys.stderr)
        return 2

    path = Path(args.from_csv)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not rows:
        print("CSV 是空的", file=sys.stderr)
        return 2

    missing = []
    for r in rows:
        for key in ("affects_reading", "over_redacted"):
            if (r.get(key) or "").strip() not in ("0", "1"):
                missing.append(f"row {r.get('row')} 的 {key}")
    if missing:
        print(f"{len(missing)} 处未填（需要 0 或 1），不生成 layer3：", file=sys.stderr)
        for m in missing[:10]:
            print("  -", m, file=sys.stderr)
        return 1

    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.get("image_id", ""), row.get("finding_id", ""))
        if key in seen:
            print(f"CSV 有重复区域：{key}", file=sys.stderr)
            return 1
        seen.add(key)
        if key not in expected:
            print(f"CSV 含来源之外的区域：{key}", file=sys.stderr)
            return 1
        expected_bbox = ",".join(str(v) for v in expected[key]["bbox"])
        if (row.get("bbox") or "").strip() != expected_bbox:
            print(f"CSV bbox 与 RedactionPlan 不一致：{key}", file=sys.stderr)
            return 1
    missing_rows = sorted(set(expected) - seen)
    if missing_rows:
        print(f"CSV 漏了 {len(missing_rows)} 个区域，例如 {missing_rows[:5]}", file=sys.stderr)
        return 1

    images = {r["image_id"] for r in rows}
    payload = {
        "status": "ok",
        "reason": "",
        "provenance": {
            "layer2_sha256": hashlib.sha256(layer2_path.read_bytes()).hexdigest(),
            "expected_regions": len(expected),
        },
        "summary": {
            "images": len(images),
            "regions_reviewed": len(rows),
            "affects_reading": sum(int(r["affects_reading"]) for r in rows),
            "over_redacted": sum(int(r["over_redacted"]) for r in rows),
        },
        "items": rows,
    }
    out = Path(args.out_json)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"layer3 已写：{out}")
    print(f"  {len(rows)} 个区域 / {len(images)} 张图；"
          f"影响阅读 {payload['summary']['affects_reading']}，"
          f"明显误遮 {payload['summary']['over_redacted']}")
    return 0


def from_image_csv(args: argparse.Namespace) -> int:
    """按**整图**判读的 CSV → layer3 JSON。

    A 的素材是 38 张整图对照（不是 52 个区域裁图）。整图判读能回答
    「有没有影响阅读」，但"误遮了几个对象"要判读人自己数，所以这一列的
    单位是**对象个数**（0..该图遮盖数），不是 0/1。
    """
    layer2_path = Path(getattr(args, "layer2", "") or "")
    if not layer2_path.is_file():
        print("--from-image-csv 必须同时提供 --layer2，否则无法证明没有漏图", file=sys.stderr)
        return 2
    layer2 = json.loads(layer2_path.read_text(encoding="utf-8"))
    if layer2.get("status") != "ok":
        print(f"第二层不是 ok（{layer2.get('status')}），不能生成第三层结论", file=sys.stderr)
        return 2
    expected: dict[str, int] = {}
    for item in layer2.get("items") or []:
        image_id = item.get("image_id")
        if not image_id or not item.get("out"):
            continue
        regions = item.get("plan_regions")
        if not isinstance(regions, list):
            print(f"[error] {image_id} 缺 plan_regions，不能用预聚合数字代替",
                  file=sys.stderr)
            return 2
        claimed = int(item.get("plan") or 0)
        if len(regions) != claimed:
            print(f"[error] {image_id} plan={claimed} 但 plan_regions={len(regions)}",
                  file=sys.stderr)
            return 2
        expected[image_id] = len(regions)
    if not expected:
        print("[error] 第二层没有可判读图片", file=sys.stderr)
        return 2

    path = Path(args.from_image_csv)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not rows:
        print("CSV 是空的", file=sys.stderr)
        return 2

    problems: list[str] = []
    seen_images: set[str] = set()
    for r in rows:
        image_id = r.get("image_id") or "?"
        if image_id in seen_images:
            problems.append(f"{image_id}: 图片重复")
        seen_images.add(image_id)
        if image_id not in expected:
            problems.append(f"{image_id}: 不在第二层结果中")
        redactions = r.get("redactions")
        try:
            n_red = int(redactions)
        except (TypeError, ValueError):
            problems.append(f"{image_id}: redactions 不是整数")
            n_red = -1
        if (r.get("affects_reading") or "").strip() not in ("0", "1"):
            problems.append(f"{image_id}: affects_reading 需要 0 或 1")
        raw = (r.get("over_redacted") or "").strip()
        try:
            n_over = int(raw)
        except ValueError:
            problems.append(f"{image_id}: over_redacted 需要整数（对象个数）")
            n_over = -1
        if n_over >= 0 and n_red >= 0 and n_over > n_red:
            problems.append(f"{image_id}: over_redacted={n_over} 超过该图遮盖数 {n_red}")
        if n_red == 0 and n_over > 0:
            problems.append(f"{image_id}: 没有遮盖却记了误遮")
        if image_id in expected and n_red != expected[image_id]:
            problems.append(
                f"{image_id}: redactions={n_red} 与第二层计划区域 {expected[image_id]} 不一致"
            )

    missing_images = sorted(set(expected) - seen_images)
    if missing_images:
        problems.append(f"漏了 {len(missing_images)} 张图：{missing_images[:8]}")

    if problems:
        print(f"{len(problems)} 处不合格，不生成 layer3：", file=sys.stderr)
        for item in problems[:12]:
            print("  -", item, file=sys.stderr)
        return 1

    payload = {
        "status": "ok",
        "reason": "",
        "review_unit": "image",
        "provenance": {
            "layer2_sha256": hashlib.sha256(layer2_path.read_bytes()).hexdigest(),
            "expected_images": len(expected),
        },
        "summary": {
            "images": len(rows),
            "regions_reviewed": sum(int(r["redactions"]) for r in rows),
            "affects_reading": sum(int(r["affects_reading"]) for r in rows),
            "over_redacted": sum(int(r["over_redacted"]) for r in rows),
        },
        "items": rows,
    }
    out = Path(args.out_json)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"layer3 已写：{out}")
    print(f"  {payload['summary']['images']} 张图 / "
          f"{payload['summary']['regions_reviewed']} 处遮盖；"
          f"影响阅读 {payload['summary']['affects_reading']} 张，"
          f"误遮 {payload['summary']['over_redacted']} 个对象")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="第三层：人工过度脱敏判读表")
    ap.add_argument("--layer2", default="", help="第二层 JSON（生成判读表时必填）")
    ap.add_argument("--images", default=str(ROOT / "dataset/dev/images"))
    ap.add_argument(
        "--predictions",
        default="",
        help="兼容旧命令保留；第三层现在直接读取 layer2.plan_regions，不使用该目录",
    )
    ap.add_argument("--out", default=str(ROOT / "results/layer3_sheet"))
    ap.add_argument(
        "--from-csv",
        default="",
        help="按区域填好的判读表；必须同时给 --layer2",
    )
    ap.add_argument(
        "--from-image-csv",
        default="",
        help="按整图填好的判读表；必须同时给 --layer2",
    )
    ap.add_argument("--out-json", default=str(ROOT / "results/layer3.json"))
    args = ap.parse_args()
    if args.from_csv:
        return from_csv(args)
    if args.from_image_csv:
        return from_image_csv(args)
    if not args.layer2:
        ap.error("生成判读表需要 --layer2")
    return build_sheet(args)


if __name__ == "__main__":
    raise SystemExit(main())
