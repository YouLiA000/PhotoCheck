"""第一层指标：检测质量（处理前）。

口径来自 K-13：
  - 对象级召回 / 精确 / 每图误报数
  - 分 subtype 报告样本数 n 与失败案例
  - 文字检出与敏感类别判定分开统计
  - 每个指标必须带 n；n < 30 标注「仅作开发参考」

`subtype_error` 是第三个数：框对上了、类别判错。它既不算漏检也不算误报，
单独一列，否则"OCR 没读出来"和"读出来了但规则判错"会被混成一个数字。
"""

from __future__ import annotations

from typing import Iterable

from . import config
from .matcher import ImageResult


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def summarize(results: Iterable[ImageResult]) -> dict:
    results = list(results)
    tp = sum(r.tp_total for r in results)
    fn = sum(r.fn_total for r in results)
    fp = sum(r.fp_total for r in results)
    subtype_errors = sum(r.subtype_errors for r in results)

    per_subtype: dict[str, dict[str, int]] = {}
    for r in results:
        for sub, counters in r.per_subtype.items():
            slot = per_subtype.setdefault(
                sub, {"tp": 0, "fn": 0, "fp": 0, "subtype_error": 0}
            )
            for key, value in counters.items():
                slot[key] = slot.get(key, 0) + value

    rows = []
    for sub in sorted(per_subtype):
        c = per_subtype[sub]
        n_truth = c["tp"] + c["fn"]      # 真值对象数（判别错的那些已经计入 tp 的位置个数）
        n_truth += c["subtype_error"]
        n_pred = c["tp"] + c["subtype_error"] + c["fp"]
        rows.append(
            {
                "subtype": sub,
                "n": n_truth,
                "tp": c["tp"],
                "fn": c["fn"],
                "fp": c["fp"],
                "subtype_error": c["subtype_error"],
                "recall": _ratio(c["tp"], n_truth),
                "precision": _ratio(c["tp"], n_pred),
                "note": config.SMALL_SAMPLE_NOTE if n_truth < config.MIN_N_FOR_CONCLUSION else "",
            }
        )

    images = len(results)
    mask_checked = sum(r.mask_checked for r in results)
    mask_bad = sum(r.mask_mismatches for r in results)
    duplicates = sum(r.duplicates for r in results)
    n_pred = tp + subtype_errors + fp
    return {
        "images": images,
        "objects_truth": tp + fn + subtype_errors,
        "objects_pred": n_pred,
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "subtype_errors": subtype_errors,
        "recall": _ratio(tp, tp + fn + subtype_errors),
        "precision": _ratio(tp, tp + subtype_errors + fp),
        # 掩码正确率：只覆盖「真值与预测都给了 masked_preview」的配对，
        # 所以分母不是全部 TP，报告里要带上 mask_checked
        "mask_checked": mask_checked,
        "mask_mismatches": mask_bad,
        "mask_accuracy": _ratio(mask_checked - mask_bad, mask_checked),
        # 重复检出：纯预测侧，与真值无关。一个号码报成两条时，
        # 老指标只会算成 1 TP + 1 FP，看不出"是重复"还是"另有一处"
        "duplicates": duplicates,
        "duplicate_rate": _ratio(duplicates, n_pred),
        "fp_per_image": round(fp / images, 3) if images else None,
        "negative_hits": sum(r.negative_hits for r in results),
        "by_subtype": rows,
        "small_sample": (tp + fn + subtype_errors) < config.MIN_N_FOR_CONCLUSION,
    }


def failing_images(results: Iterable[ImageResult], limit: int = 10) -> list[str]:
    """失败案例：按「漏检 + 误报」降序，给 R-02 的失败案例库用。

    **必须用 `fp_total`**：元数据误报（干净图上报了设备型号）也是误报。
    早先只数 `false_positives`，漏掉 meta 那一类，于是"唯一错误是元数据多报"的图
    永远进不了失败清单——而这恰恰是误报率最容易被拉高的一种。
    """
    rows = [(r.fn_total + r.fp_total, r.image_id) for r in results]
    rows = [row for row in rows if row[0] > 0]
    rows.sort(key=lambda x: (-x[0], x[1]))
    return [image_id for _, image_id in rows[:limit]]


def layer2_placeholder() -> dict:
    """第二层（脱敏有效性）没有数据时的显式失败，不冒充通过。"""
    return {
        "status": "未接入",
        "reason": "需要最终导出文件与逐区域复检结果；用 tools/run_layer2.py 产出后经 --layer2 注入",
        "items": [],
        "summary": {},
    }


def layer3_placeholder() -> dict:
    """第三层（过度脱敏）需要脱敏后的图 + 人工判读表。"""
    return {
        "status": "未接入",
        "reason": "需要 RedactionPlan 与处理后的图；人工「是否影响阅读」另行登记",
        "items": [],
        "summary": {},
    }


def load_layer(path: str, layer: str) -> dict:
    """读入第二/三层结果，并检查必需字段。

    **不接受空壳**：`status: ok` 但 `summary` 里没有任何分母，等于用空表换通过。
    这类"看起来有文件"的假数据比没有文件更危险，所以在入口直接拒绝。
    """
    import json as _json
    from pathlib import Path as _Path

    data = _json.loads(_Path(path).read_text(encoding="utf-8"))
    for key in ("status", "items", "summary"):
        if key not in data:
            raise ValueError(f"{layer} 结果缺少字段 {key!r}：{path}")
    if data["status"] == "ok":
        summary = data.get("summary") or {}
        if not summary.get("images"):
            raise ValueError(f"{layer} 声称 ok 但 summary.images 为空，拒绝空表通过：{path}")
    return data


def by_difficulty(results: Iterable[ImageResult], difficulty_of: dict[str, str]) -> list[dict]:
    """按 easy / hard 分层。

    K-13 要求困难样本 ≥30%，但**只报总表会把难度贡献藏起来**：一个 0.95 的召回，
    可能是 40 张清晰正拍撑起来的，也可能是困难样本真的被解决了。这两种结论对项目
    的意义完全不同，混在一起就说不清。
    """
    groups: dict[str, list[ImageResult]] = {}
    for r in results:
        groups.setdefault(difficulty_of.get(r.image_id, "unknown"), []).append(r)

    rows = []
    for name in ("easy", "hard", "unknown"):
        rs = groups.get(name)
        if not rs:
            continue
        tp = sum(r.tp_total for r in rs)
        fn = sum(r.fn_total for r in rs)
        fp = sum(r.fp_total for r in rs)
        se = sum(r.subtype_errors for r in rs)
        n = tp + fn + se
        rows.append({
            "difficulty": name,
            "images": len(rs),
            "n": n,
            "tp": tp, "fn": fn, "fp": fp, "subtype_error": se,
            "recall": _ratio(tp, n),
            "precision": _ratio(tp, tp + se + fp),
            "note": config.SMALL_SAMPLE_NOTE if n < config.MIN_N_FOR_CONCLUSION else "",
        })
    return rows


def bootstrap_ci(results: Iterable[ImageResult], n_boot: int = 1000, seed: int = 7) -> dict:
    """按**图**重采样的 95% 置信区间（cluster bootstrap）。

    为什么不按对象重采样：同一张图里的对象高度相关（同一版式、同一光照、同一次压缩），
    当成独立样本会把区间算窄，看着"很确定"，其实不是。

    为什么需要它：n=68 时召回 0.94 的区间可能是 [0.86, 1.00]，差一个对象就跳一格。
    只报点估计，等于把"样本太小"这件事藏起来。
    """
    import random

    rs = list(results)
    if not rs:
        return {"recall": [None, None], "precision": [None, None],
                "n_boot": n_boot, "seed": seed, "method": "cluster_by_image"}

    rng = random.Random(seed)
    recalls: list[float] = []
    precisions: list[float] = []
    for _ in range(n_boot):
        sample = [rs[rng.randrange(len(rs))] for _ in rs]
        tp = sum(r.tp_total for r in sample)
        fn = sum(r.fn_total for r in sample)
        fp = sum(r.fp_total for r in sample)
        se = sum(r.subtype_errors for r in sample)
        if tp + fn + se:
            recalls.append(tp / (tp + fn + se))
        if tp + se + fp:
            precisions.append(tp / (tp + se + fp))

    def percentile(xs: list[float]) -> list:
        if not xs:
            return [None, None]
        xs = sorted(xs)
        lo = xs[int(0.025 * (len(xs) - 1))]
        hi = xs[int(0.975 * (len(xs) - 1))]
        return [round(lo, 4), round(hi, 4)]

    return {
        "recall": percentile(recalls),
        "precision": percentile(precisions),
        "n_boot": n_boot,
        "seed": seed,
        "method": "cluster_by_image",
    }
