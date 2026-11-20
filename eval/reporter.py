"""三张表的输出（K-13）。

表一已能自动出数；表二、表三**故意留空但保留结构**——它们的输入还不存在
（需要 B 的导出文件与处理后的图），先占好位置，接上时不用改下游。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

DETECTION_HEADERS = ["subtype", "n", "tp", "fn", "fp", "subtype_error",
                     "recall", "precision", "note"]
DIFFICULTY_HEADERS = ["difficulty", "images", "n", "tp", "fn", "fp", "subtype_error",
                      "recall", "precision", "note"]
FAILURE_HEADERS = ["kind", "image_id", "truth_id", "truth_subtype", "pred_subtype",
                   "iou", "bbox", "in_negative"]
VALIDITY_HEADERS = ["检查项目", "通过", "失败", "状态", "说明"]
OVER_HEADERS = ["项目", "数值", "状态", "说明"]


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(out_dir: Path, summary: dict, layer2: dict, layer3: dict,
                  context: dict, failures: list[dict] | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    paths["detection"] = out_dir / "detection_quality.csv"
    _write_csv(paths["detection"], DETECTION_HEADERS, summary["by_subtype"])

    paths["validity"] = out_dir / "redaction_validity.csv"
    # B 在 main 上加了 items 支持：调用方给了逐项结果就直接写，没给才退回四行固定口径。
    validity_rows = layer2.get("items") or [
        {"检查项目": "文字是否仍可读", "状态": layer2["status"], "说明": layer2["reason"]},
        {"检查项目": "二维码是否仍可解", "状态": layer2["status"], "说明": layer2["reason"]},
        {"检查项目": "元数据是否清干净", "状态": layer2["status"], "说明": layer2["reason"]},
        {"检查项目": "残留归因三分类", "状态": layer2["status"],
         "说明": "漏检 / 用户保留 / 遮挡失败，三者不可合并"},
    ]
    _write_csv(paths["validity"], VALIDITY_HEADERS, validity_rows)

    paths["over"] = out_dir / "over_redaction.csv"
    over_rows = layer3.get("items") or [
        {"项目": "误遮对象数", "状态": layer3["status"], "说明": layer3["reason"]},
        {"项目": "每图误遮数", "状态": layer3["status"], "说明": layer3["reason"]},
        {"项目": "是否影响非敏感内容阅读", "状态": layer3["status"], "说明": "需人工登记"},
    ]
    _write_csv(paths["over"], OVER_HEADERS, over_rows)

    # 分层表：总表会把难度贡献藏起来，分开报才看得出"是不是靠简单图撑的"
    if summary.get("by_difficulty"):
        paths["difficulty"] = out_dir / "detection_by_difficulty.csv"
        _write_csv(paths["difficulty"], DIFFICULTY_HEADERS, summary["by_difficulty"])

    # 失败清单：A 的 R-02 直接吃这个文件，不用再从报表倒推
    if failures is not None:
        paths["failures"] = out_dir / "failure_cases.csv"
        _write_csv(paths["failures"], FAILURE_HEADERS, [_flatten(f) for f in failures])

    paths["json"] = out_dir / "summary.json"
    # newline="\n"：报告是给人对比 diff 的，Windows 默认 CRLF 会让同一份结果在
    # 两个平台产生不同字节，也会把 git diff 刷满整文件。
    paths["json"].write_text(
        json.dumps({"context": context, "layer1": summary, "layer2": layer2, "layer3": layer3},
                   ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )

    paths["readme"] = out_dir / "README.md"
    paths["readme"].write_text(_render_markdown(summary, context, layer2, layer3),
                               encoding="utf-8", newline="\n")
    return paths


def _flatten(failure: dict) -> dict:
    """bbox 摊成 `x,y,w,h` 字符串——CSV 里塞列表会被 Excel 拆成好几列。"""
    row = dict(failure)
    bbox = row.get("bbox")
    row["bbox"] = "" if not bbox else ",".join(str(v) for v in bbox)
    row["in_negative"] = int(bool(row.get("in_negative", False)))
    return row


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def _pair(ci_pair) -> str:
    """区间格式化。精度分母可能恒为 0（没有任何预测），那时返回"—"而不是 "None"。"""
    if not ci_pair or ci_pair[0] is None:
        return "—"
    return f"[{ci_pair[0]}, {ci_pair[1]}]"


def _ci_text(ci: dict | None) -> str:
    if not ci or not ci.get("recall"):
        return ""
    if ci["recall"][0] is None:
        return ""
    return (f"（95% CI 按图重采样 {ci.get('n_boot', '?')} 次："
            f"召回 {_pair(ci['recall'])}　精确 {_pair(ci.get('precision'))}）")


def _scope_label(context: dict) -> str:
    """报告里必须写清评了哪些 kind，否则两个数字没法比。"""
    kinds = context.get("kinds", "all")
    if kinds == "all":
        return "全部 kind"
    return "仅 " + "、".join(kinds) + "（其余 kind 不计入分母）"


def _layer2_lines(layer2: dict) -> list[str]:
    """第二层：真实导出文件 + 逐区域复检。没有数据就明说，不写"通过"。"""
    if layer2.get("status") != "ok":
        return [f"**未接入**：{layer2.get('reason') or '没有数据'}"]
    m = layer2.get("summary") or {}
    rows = [
        f"- 数据源：最终导出文件 + 逐区域复检（{m.get('images', 0)} 张图，"
        f"{m.get('regions', 0)} 个计划区域）",
        "",
        "| 检查项 | 数值 | 分母 |",
        "|---|---:|---:|",
        f"| 计划区域 | {m.get('regions', 0)} | {m.get('images', 0)} 图 |",
        f"| 复检通过 | {m.get('verification_passed', 0)} | {m.get('verification_checks', 0)} |",
        f"| 复检失败 | {m.get('verification_failed', 0)} | {m.get('verification_checks', 0)} |",
        f"| 残留项 | {m.get('residuals', 0)} | — |",
        f"| **导出文件元数据残留** | **{m.get('export_metadata_residual', 0)}** | "
        f"{m.get('export_metadata_checked', 0)} |",
        f"| 未执行处置（not_applicable） | {m.get('images_not_applicable', 0)} | "
        f"{m.get('images', 0)} |",
        "",
        "> 元数据判据只查 EXIF / XMP / IPTC / MakerNotes；ICC profile 的 "
        "`Device Model` 不算残留（那是色彩管理字段，不含拍摄信息）。",
    ]
    return rows


def _layer3_lines(layer3: dict) -> list[str]:
    """第三层：过度脱敏。必须人工判读，脚本只汇总。"""
    if layer3.get("status") != "ok":
        return [f"**未接入**：{layer3.get('reason') or '需要人工判读'}"
                "（没有人工数据时不生成数字，也不拿自动代理指标冒充）"]
    m = layer3.get("summary") or {}
    return [
        f"- 人工判读 {m.get('regions_reviewed', 0)} 个区域 / {m.get('images', 0)} 张图",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 影响非敏感内容阅读 | {m.get('affects_reading', 0)} |",
        f"| 误遮对象数 | {m.get('over_redacted', 0)} |",
    ]


def _render_markdown(summary: dict, context: dict, layer2: dict | None = None,
                     layer3: dict | None = None) -> str:
    # 默认值保持向后兼容：老调用方只传 summary/context 时按"未接入"渲染，
    # 而不是抛 TypeError。
    layer2 = layer2 or {"status": "未接入", "reason": "未提供第二层结果", "items": [], "summary": {}}
    layer3 = layer3 or {"status": "未接入", "reason": "未提供第三层人工判读", "items": [], "summary": {}}
    lines = [
        "# 评测结果",
        "",
        f"- 数据集：`{context['dataset']}`（{summary['images']} 张有标注的图）",
        f"- 预测来源：{context['prediction']}",
        f"- 匹配口径：IoU ≥ {context['iou_threshold']} 且 subtype 相同（K-13）",
        f"- 评估范围：{_scope_label(context)}",
        f"- 生成时间：{context['generated_at']}　git：`{context['git_sha']}`"
        + ("　**工作区有未提交改动**" if context.get("git_dirty") else ""),
        "",
    ]
    cov = summary.get("coverage")
    if cov and (cov["uncovered_images"] or cov["foreign_count"]):
        # 覆盖率不对时，提醒放在最前面：下面的分数只代表「被覆盖的那部分」
        detail = []
        if cov["uncovered_images"]:
            detail.append(f"{cov['uncovered_images']} 张真值图没有任何预测（算作漏检）")
        if cov["foreign_count"]:
            sample = "、".join(cov["foreign_images"][:3])
            detail.append(f"{cov['foreign_count']} 张预测的 image_id 不在数据集里（如 {sample}）")
        # 真值图全有预测、只是预测多了几张（例如对无标签的坏图也跑了），
        # 不是"覆盖不完整"，别把两种问题混成一句。
        if cov["uncovered_images"]:
            headline = f"覆盖不完整（{cov['matched_images']}/{cov['truth_images']} 张）"
        else:
            headline = "真值图全部有预测，但预测里有多余的图"
        lines += [
            f"> ⚠ **{headline}**：" + "；".join(detail) + "。下面的指标只反映已对齐的部分。",
            "",
        ]
    lines += [
        "## 第一层 · 检测质量",
        "",
        f"真值对象 {summary['objects_truth']} 个，预测 {summary['objects_pred']} 个 → "
        f"TP {summary['tp']} / FN {summary['fn']} / FP {summary['fp']} / 类别判错 {summary['subtype_errors']}",
        "",
        f"**召回 {_fmt(summary['recall'])}　精确 {_fmt(summary['precision'])}　"
        f"每图误报 {_fmt(summary['fp_per_image'])}**",
        "",
    ]
    if summary.get("mask_checked"):
        lines += [
            f"掩码正确率 {_fmt(summary['mask_accuracy'])}"
            f"（{summary['mask_checked'] - summary['mask_mismatches']}/{summary['mask_checked']}"
            f"，只统计真值与预测都给了 `masked_preview` 的配对）",
            "",
        ]
    if summary.get("duplicates"):
        lines += [
            f"**重复检出 {summary['duplicates']} 条**（占预测 {_fmt(summary['duplicate_rate'])}）。"
            "同一 subtype、IoU ≥ 0.5 的两个预测框记后者为多余——"
            "老指标只会把它算成 1 TP + 1 FP，看不出是重复。",
            "",
        ]
    ci_line = _ci_text(summary.get("ci"))
    if ci_line:
        lines += [ci_line, ""]
    lines += [
        "| subtype | n | tp | fn | fp | 类别判错 | recall | precision | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["by_subtype"]:
        lines.append(
            f"| {row['subtype']} | {row['n']} | {row['tp']} | {row['fn']} | {row['fp']} | "
            f"{row['subtype_error']} | {_fmt(row['recall'])} | {_fmt(row['precision'])} | {row['note']} |"
        )
    if summary["small_sample"]:
        lines += ["", "> 总样本数不足 30，以上数值**仅作开发参考**，不得据此下通用结论。"]

    if summary.get("by_difficulty"):
        lines += ["", "### 按难度分层", "",
                  "| 难度 | 图数 | n | tp | fn | fp | 类别判错 | recall | precision | 备注 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for row in summary["by_difficulty"]:
            lines.append(
                f"| {row['difficulty']} | {row['images']} | {row['n']} | {row['tp']} | "
                f"{row['fn']} | {row['fp']} | {row['subtype_error']} | "
                f"{_fmt(row['recall'])} | {_fmt(row['precision'])} | {row['note']} |"
            )
        lines += ["", "> 总表与分层表口径相同；分层是为了看清难度贡献，不是另一套指标。"]
    loose = summary.get("loose")
    if loose:
        lines += [
            "",
            "## 附录 · 敏感性分析（不进主表）",
            "",
            f"主口径 IoU ≥ {context['iou_threshold']}；这里把**文本类**放宽到 "
            f"{loose['threshold']}（二维码 / 人脸仍用主口径）。",
            "",
            "| 口径 | 召回 | 精确 | tp | fn | fp | 类别判错 |",
            "|---|---|---|---|---|---|---|",
            f"| 主口径 {context['iou_threshold']} | {_fmt(summary['recall'])} | "
            f"{_fmt(summary['precision'])} | {summary['tp']} | {summary['fn']} | "
            f"{summary['fp']} | {summary['subtype_errors']} |",
            f"| 宽口径 {loose['threshold']}（仅文本） | {_fmt(loose['recall'])} | "
            f"{_fmt(loose['precision'])} | {loose['tp']} | {loose['fn']} | "
            f"{loose['fp']} | {loose['subtype_errors']} |",
            "",
            "> 主结论只认主口径。两档一致 = 结论对阈值不敏感；宽口径明显变好 = "
            "框偏大但位置对，属于检测框质量问题，不是漏检。",
        ]

    lines += ["", "## 第二层 · 脱敏有效性", ""]
    lines += _layer2_lines(layer2)
    lines += ["", "## 第三层 · 过度脱敏", ""]
    lines += _layer3_lines(layer3)
    lines += [
        "",
        "## 口径提醒",
        "",
        "- `类别判错` 是「框对上了但 subtype 判错」，既不算漏检也不算误报。",
        "- `误报` 只统计画面上的对象；元数据项按 subtype 在整图范围内配对。",
        "- 本表不含首次漏检：同一套模型看不见的东西，重跑也看不见（见 R-03）。",
        "- 置信区间按**图**重采样（同一张图里的对象不独立），不是按对象。",
        "- `failure_cases.csv` 逐条列出漏检 / 误报 / 类别判错，可直接作为失败案例库的输入。",
    ]
    return "\n".join(lines) + "\n"
