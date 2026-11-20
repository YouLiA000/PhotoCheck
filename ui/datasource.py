"""检测数据源与展示层（W3 骨架 + W6 风险解释）。

**不另写 mock 数据**：`dataset/dev/labels/*.json` 本身就是合法的契约 v4 记录，
直接当 Detection 用。好处是 W5 接 A 的真实输出时只换这一个模块，界面一行都不用动；
坏处是如果真值格式错了，界面也会跟着错——所以它先过 `validate_records`。

风险分值**不读 Detection 里存的值**，而是调 `contract/risk_rules.py` 重算：
`Detection.risk` 是必填字段，A 可能填错；界面重算才能当场发现对不上（见 `mismatches`）。
措辞一律走 `contract/risk_explain.py`，本模块不拼句子——K-10 验收要求。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from contract.risk_explain import (  # noqa: E402
    KIND_LABEL,
    SUBTYPE_LABEL,
    RiskExplanation,
    explain,
    evidence_of,
    label_of,
    panel_markdown as _panel_markdown,
)
from contract.risk_rules import resolve_risk  # noqa: E402
from contract.validate_records import validate_records  # noqa: E402

DEV = ROOT / "dataset" / "dev"
LABELS = DEV / "labels"
IMAGES = DEV / "images"

RISK_COLOR = {3: (214, 40, 40), 2: (240, 150, 20), 1: (110, 130, 160)}

__all__ = [
    "KIND_LABEL", "SUBTYPE_LABEL", "RISK_COLOR", "RiskExplanation",
    "list_samples", "image_path", "load_records", "detections", "severity",
    "display_rows", "draw_boxes", "panel_markdown",
]


def list_samples() -> list[str]:
    return sorted(p.stem for p in LABELS.glob("*.json"))


def image_path(image_id: str) -> Path:
    return IMAGES / f"{image_id}.jpg"


def load_records(image_id: str) -> list[dict]:
    """读取并校验真值；错误只含 code/path，避免把未掩码值写进异常。"""
    path = LABELS / f"{image_id}.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取真值 {image_id}.json：{type(exc).__name__}") from exc
    if not isinstance(records, list):
        raise ValueError(f"真值 {image_id}.json 必须是记录数组")
    report = validate_records(records)
    if not report.ok:
        issues = report.errors[:3]
        detail = "；".join(f"{issue.code} @ {issue.path}" for issue in issues)
        more = f"；另有 {len(report.errors) - len(issues)} 条" if len(report.errors) > 3 else ""
        raise ValueError(f"真值 {image_id} 不符合契约 v4：{detail}{more}")
    return records


def detections(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("record_type") == "Detection"]


def severity(records: list[dict]) -> RiskExplanation:
    """整图风险解释。返回带明细的对象，界面与报告共用同一份措辞。"""
    return explain(detections(records))


def display_rows(records: list[dict]) -> list[list]:
    """清单表：**只放掩码，不放原文**（规格卡 §0.5 红线 1）。

    第 4 列显示**分值本身**（1/2/3），不翻译成高/中/低：整图才分级，单对象只给分。
    混用两套词会让人把「某个手机号是高风险」读成「这张图高风险」。
    第 5 列「来源」是 K-10 的硬要求（severity 与 evidence 分两列），
    没有它就分不清「EXIF 直读的」和「OCR 猜的」哪个更可信。
    """
    exp = explain(detections(records))
    mismatch_at = {m[0] for m in exp.mismatches}

    rows = []
    for i, rec in enumerate(detections(records), start=1):
        details = rec.get("details") or {}
        shown = details.get("masked_preview") or details.get("masked_summary") or "—"
        bbox = rec.get("bbox")
        risk = _resolved(rec)
        mark = " *" if str(rec.get("id")) in mismatch_at else ""
        rows.append([
            i,
            KIND_LABEL.get(rec.get("kind", ""), rec.get("kind", "—")),
            label_of(rec),
            str(risk) + mark,
            evidence_of(rec),
            shown,
            "整图" if bbox is None else f"{bbox[0]},{bbox[1]},{bbox[2]}×{bbox[3]}",
        ])
    return rows


def _resolved(rec: dict) -> int:
    """按规则算的分；规则没定义的 subtype 退回自带值（界面不能因为多一类就崩）。

    逻辑在 `contract/risk_rules.resolve_risk`，这里只取分值——界面不能有第二份
    「规则没定义时怎么办」的判断，否则和面板会走岔。
    """
    return resolve_risk(rec)[0]


def draw_boxes(image: Image.Image, records: list[dict]) -> Image.Image:
    """在图上画检测框，颜色按**规则算的**风险，不按检测器自带值。

    K-11 工具表写的是 AnnotatedImage，但它的数据形态是「一组裁剪图 + 标签」，
    按框配色和编号不好做。规格卡本身给了降级路径：
    「AnnotatedImage 不好用 → 在图上直接画框的静态图 + 单独列表控件」，这里走这条。

    **尺寸按图的大小缩放**：界面把 1120×1560 的竖图压到 360px 高显示（K-11 要求
    1280×800 不滚动），固定的 3px 框线和 20px 字号会被缩到半像素，编号糊成一团。
    框线按长边 1/450、字号按长边 1/40 计算，缩略后仍然看得清。
    """
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    long_side = max(canvas.width, canvas.height)
    font_size = max(16, round(long_side / 40))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", font_size)
    except OSError:
        font = ImageFont.load_default()

    pad = max(3, round(long_side / 400))
    line_w = max(3, round(long_side / 300))
    label_h = font_size + 8

    for idx, rec in enumerate(detections(records), start=1):
        bbox = rec.get("bbox")
        if not bbox:
            continue
        x, y, w, h = bbox
        color = RISK_COLOR.get(_resolved(rec), RISK_COLOR[1])
        draw.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], outline=color, width=line_w)

        text = str(idx)
        probe = draw.textbbox((0, 0), text, font=font)
        box_w = max(label_h, probe[2] - probe[0] + 12)
        # 上方放不下就压进框内左上角——宁可盖住一小块，也不要被画布切掉
        ty = y - pad - label_h
        if ty < 0:
            ty = max(0, y - pad)
        tx = max(0, x - pad)
        draw.rectangle([tx, ty, tx + box_w, ty + label_h], fill=color)
        draw.text((tx + (box_w - (probe[2] - probe[0])) / 2, ty + (label_h - (probe[3] - probe[1])) / 2 - probe[1]),
                  text, font=font, fill=(255, 255, 255))
    return canvas


def panel_markdown(records: list[dict], plan_revision: int | None = None) -> str:
    """结论面板（K-12）。整段委托给 risk_explain，本模块不拼任何措辞。"""
    return _panel_markdown(detections(records), plan_revision=plan_revision)
