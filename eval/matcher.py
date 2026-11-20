"""真值框与预测框的匹配（K-13 第一层）。

三条纪律：
  1. IoU >= 0.5 且 subtype 相同才算命中；
  2. **一对一**，一个真值框只能被一个预测框匹配，反之亦然；
  3. 位置对了但 subtype 判错，单列成「判别错」，不能算漏检也不能算误报——
     这是规格卡要求「文字检出与敏感类别判定分开统计」的落点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import config


def iou(a: Sequence[int], b: Sequence[int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def center_inside(box: Sequence[int], region: Sequence[int]) -> bool:
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    rx, ry, rw, rh = region
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


def _mask_of(record: Mapping) -> str | None:
    details = record.get("details") or {}
    value = details.get("masked_preview")
    return str(value) if value else None


def _check_mask(result: "ImageResult", image_id: str, truth: Mapping, pred: Mapping) -> None:
    """比对配对上的 `masked_preview`。

    为什么要单独统计：框对、类别对，**掩码却可能是错的**。2026-11-07 的实拍里，
    手写证件号被读成另一个号，掩码末四位跟着错（`…1234` 变 `…4010`），
    但召回/精确全是满分——三项老指标一个都看不见它。
    """
    t_mask, p_mask = _mask_of(truth), _mask_of(pred)
    if not t_mask or not p_mask:
        return
    result.mask_checked += 1
    if t_mask != p_mask:
        result.mask_mismatches += 1
        result.failures.append({
            "kind": "mask_mismatch", "image_id": image_id,
            "truth_id": truth.get("id"), "truth_subtype": truth.get("subtype"),
            "pred_subtype": pred.get("subtype"), "iou": None,
            "truth_mask": t_mask, "pred_mask": p_mask,
            "bbox": truth.get("bbox"),
        })


@dataclass
class ImageResult:
    """一张图的匹配结果。计数都是「对象数」，不是「图数」。"""

    image_id: str
    true_positives: int = 0          # 框对、类别对
    subtype_errors: int = 0          # 框对、类别错
    misses: int = 0                  # 真值没被任何预测框对上
    false_positives: int = 0         # 预测框没对上任何真值
    negative_hits: int = 0           # 误报中，落在「像但不是」区域里的个数
    meta_true_positives: int = 0
    meta_misses: int = 0
    meta_false_positives: int = 0
    # 掩码正确率：只统计「真值给了 masked_preview 且预测也给了」的配对
    mask_checked: int = 0
    mask_mismatches: int = 0
    # 重复检出：同一 subtype、IoU>=0.5 的两个预测框，后一个记为多余。
    # 这是**纯预测侧**统计，不需要真值——2026-11-07 实拍上发现的
    # 「一个号码报成两条」就是它，靠召回/精确都看不出来（会被算成 1 TP + 1 FP）。
    duplicates: int = 0
    per_subtype: dict[str, dict[str, int]] = field(default_factory=dict)
    # 逐条失败明细：只给计数没法查，报告里必须能追到"哪一项错了、错在哪"
    failures: list[dict] = field(default_factory=list)

    def bump(self, subtype: str, key: str, n: int = 1) -> None:
        self.per_subtype.setdefault(subtype, {})[key] = (
            self.per_subtype.setdefault(subtype, {}).get(key, 0) + n
        )

    @property
    def tp_total(self) -> int:
        return self.true_positives + self.meta_true_positives

    @property
    def fn_total(self) -> int:
        return self.misses + self.meta_misses

    @property
    def fp_total(self) -> int:
        return self.false_positives + self.meta_false_positives


def _split(detections: Sequence[Mapping]):
    boxes = [d for d in detections if d.get("bbox")]
    metas = [d for d in detections if not d.get("bbox")]
    return boxes, metas


def match_image(
    image_id: str,
    truth: Sequence[Mapping],
    pred: Sequence[Mapping],
    negatives: Sequence[Mapping] = (),
    thresholds: Mapping[str, float] | None = None,
) -> ImageResult:
    """`thresholds` 按 kind 覆盖 IoU 阈值，用于敏感性分析。

    只放宽文本类（细长条，OCR 框常比字形框大一圈）；二维码/人脸是真矩形，框歪了就是歪了，
    放宽等于把明显的错配也判成命中，那一档就没意义了。
    """
    result = ImageResult(image_id=image_id)
    t_boxes, t_metas = _split(truth)
    p_boxes, p_metas = _split(pred)

    # ---- 有框对象：按 IoU 从高到低贪心配对，保证一对一 ----
    candidates = []
    for ti, t in enumerate(t_boxes):
        threshold = (thresholds or {}).get(str(t.get("kind")), config.IOU_THRESHOLD)
        for pi, p in enumerate(p_boxes):
            score = iou(t["bbox"], p["bbox"])
            if score >= threshold:
                candidates.append((score, ti, pi))
    candidates.sort(key=lambda c: -c[0])

    used_t, used_p = set(), set()
    for score, ti, pi in candidates:
        if ti in used_t or pi in used_p:
            continue
        used_t.add(ti)
        used_p.add(pi)
        t, p = t_boxes[ti], p_boxes[pi]
        _check_mask(result, image_id, t, p)
        if t["subtype"] == p["subtype"]:
            result.true_positives += 1
            result.bump(t["subtype"], "tp")
        else:
            # 位置对上了，类别判错 —— 这是「OCR 检出了但规则判错」，不是漏检
            result.subtype_errors += 1
            result.bump(t["subtype"], "subtype_error")
            result.failures.append({
                "kind": "subtype_error", "image_id": image_id,
                "truth_id": t.get("id"), "truth_subtype": t["subtype"],
                "pred_subtype": p["subtype"], "iou": round(score, 3),
                "bbox": t.get("bbox"),
            })

    # ---- 重复检出：预测侧自重叠，与真值无关 ----
    dup = set()
    for i in range(len(p_boxes)):
        if i in dup:
            continue
        for j in range(i + 1, len(p_boxes)):
            if j in dup:
                continue
            if (p_boxes[i].get("subtype") == p_boxes[j].get("subtype")
                    and iou(p_boxes[i]["bbox"], p_boxes[j]["bbox"]) >= config.IOU_THRESHOLD):
                dup.add(j)
    result.duplicates = len(dup)
    for j in sorted(dup):
        result.failures.append({
            "kind": "duplicate", "image_id": image_id,
            "truth_id": None, "truth_subtype": None,
            "pred_subtype": p_boxes[j].get("subtype"), "iou": None,
            "bbox": p_boxes[j].get("bbox"),
        })

    for ti, t in enumerate(t_boxes):
        if ti not in used_t:
            result.misses += 1
            result.bump(t["subtype"], "fn")
            result.failures.append({
                "kind": "miss", "image_id": image_id,
                "truth_id": t.get("id"), "truth_subtype": t["subtype"],
                "pred_subtype": None, "iou": None, "bbox": t.get("bbox"),
            })

    for pi, p in enumerate(p_boxes):
        if pi not in used_p:
            result.false_positives += 1
            result.bump(p["subtype"], "fp")
            hit_negative = any(center_inside(p["bbox"], n["bbox"]) for n in negatives)
            if hit_negative:
                result.negative_hits += 1
            result.failures.append({
                "kind": "false_positive", "image_id": image_id,
                "truth_id": None, "truth_subtype": None,
                "pred_subtype": p["subtype"], "iou": None,
                "bbox": p.get("bbox"), "in_negative": hit_negative,
            })

    # ---- 元数据：没有 bbox，按 subtype 在整图范围内配 ----
    t_count: dict[str, int] = {}
    for m in t_metas:
        t_count[m["subtype"]] = t_count.get(m["subtype"], 0) + 1
    for m in p_metas:
        sub = m["subtype"]
        if t_count.get(sub, 0) > 0:
            t_count[sub] -= 1
            result.meta_true_positives += 1
            result.bump(sub, "tp")
        else:
            result.meta_false_positives += 1
            result.bump(sub, "fp")
            result.failures.append({
                "kind": "false_positive", "image_id": image_id, "truth_id": None,
                "truth_subtype": None, "pred_subtype": sub, "iou": None, "bbox": None,
            })
    for sub, left in t_count.items():
        if left > 0:
            result.meta_misses += left
            result.bump(sub, "fn", left)
            for _ in range(left):
                result.failures.append({
                    "kind": "miss", "image_id": image_id, "truth_id": None,
                    "truth_subtype": sub, "pred_subtype": None, "iou": None, "bbox": None,
                })

    return result
