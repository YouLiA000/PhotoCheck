"""R-04 second-OCR and two-person manual review evidence."""

from __future__ import annotations

import json

from audit.model import Context, Result, check, fail, ok, partial, skip


@check("R04-01", "R-04", "第二 OCR 交叉复核与双人人工判读", level="P1", owner="B")
def second_ocr_and_manual(ctx: Context) -> Result:
    result_path = ctx.docs_dir / "R04_第二OCR结果_2026-11-15.json"
    if not result_path.is_file():
        return skip("尚无 R-04 结果", "运行 tools/r04_second_ocr.py")
    try:
        payload = json.loads(ctx.read_text(result_path))
        summary = payload["summary"]
        sample_size = int(payload["sample_size"])
        agreement = float(summary["agreement_rate"])
        control = float(summary["control_readable_rate"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(f"R-04 结果不可读：{type(exc).__name__}", "重跑第二 OCR 抽样")

    if sample_size < 30:
        return fail(f"仅 {sample_size} 个对象", "规格卡要求 30 个已处理对象")
    if agreement < 0.9:
        return fail(f"同模型与 Tesseract 一致率 {agreement:.1%}", "逐条检查分歧")
    if control < 0.8:
        return fail(f"第二 OCR 原图正控仅 {control:.1%}", "先确认 Tesseract 当前识别能力")

    manual_path = ctx.docs_dir / "manual-summary.json"
    if not manual_path.is_file():
        return partial(
            f"自动交叉 {agreement:.1%}，正控 {control:.1%}；双人判读未填写",
            "两位评审独立填写 R04 对象表后运行 tools/finalize_r04_manual.py",
        )
    try:
        manual = json.loads(ctx.read_text(manual_path))
    except json.JSONDecodeError:
        return partial("人工摘要不是合法 JSON", "重新运行人工汇总脚本")
    if manual.get("manual_review_status") != "complete_two_independent_reviewers":
        return partial("人工判读摘要未完成", "补齐两位评审与分歧裁决")
    return ok(
        f"{sample_size} 对象；自动交叉 {agreement:.1%}；"
        f"人工一致 {manual.get('reviewer_agreement_rate')}"
    )
