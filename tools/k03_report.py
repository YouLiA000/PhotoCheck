#!/usr/bin/env python3
"""K-03 性能报告：JSON 模板、汇总统计与校验器。

依据：三方共识最终意见（B 最终意见 v2，2026-11-15）§2。

K-03 测的是**纯 OCR 稳态耗时**，不是端到端检测。测量由 A 的引擎执行，但报告的
**字段结构、汇总口径、缺什么算无效**是共识定死的，C 负责固化成这一个模块，
避免三方各写一份口径、同一份报告三个判法。

分三层：
  1. `compute_aggregates`：把「每图多次原始耗时」汇总成共识要求的统计量
     （median_of_image_medians / p95 / max / n_images / 图内极差中位数 / bootstrap CI）。
  2. `validate_report`：按模板校验报告字段齐全、口径一致；缺必需字段直接判无效。
  3. CLI：`validate` 校验一份报告、`template` 输出空模板，方便 A 照着填。

只依赖标准库。这里把规格卡的 1.2× / 1.5× 闸门固化为可执行判定，但冻结基线
仍由 `T_OCR_REF_V2_MS` 明确控制；改基线或参考集必须升级版本并取得三方同意。

用法：
    python tools/k03_report.py template > k03_report.json     # 出空模板
    python tools/k03_report.py validate k03_report.json      # 校验，缺字段报错
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 共识常量与冻结参考集基线
# ---------------------------------------------------------------------------

# 旧基线：仅作预验收参考，不得命名为新参考集基线（B 最终意见 §2.2）
T_OCR_LEGACY_MS = 1686.3
# 旧基线的 p95 软闸门值（1686.3 × 1.5 = 2529.45，统一保守写 2529.5）
LEGACY_P95_LINE_MS = 2529.5
# k03_ref_v1 冻结值，来自 2026-11-15 的参考集测量并由三方最终口径确认。
T_OCR_REF_V2_MS = 2690.2

# 闸门倍数（B 最终意见 §2.3）：median ≤1.2×、p95 ≤1.5×、max 不设硬线但 >2× 须逐图解释
GATE_MEDIAN_MULT = 1.2
GATE_P95_MULT = 1.5
GATE_MAX_MULT = 2.0

# 固定参考集（B 最终意见 §2.2 表）：ID -> (宽, 高, 内容, 绘制字符数)
REFERENCE_SET: tuple[tuple[str, int, int, str, int], ...] = (
    ("R1", 900, 620, "稀疏，4 行短文本", 76),
    ("R2", 900, 620, "密集，16 行长文本", 864),
    ("R3", 1120, 1560, "稀疏，5 行短文本", 95),
    ("R4", 1120, 1560, "密集，20 行长文本", 1080),
    ("R5", 1600, 1200, "中等，15 行长文本", 810),
)

DATA_VERSION = "k03_ref_v1"

# 每图至少跑几次（B 最终意见 §2.1.2：每图先作废 1 次，随后至少 3 次）
MIN_REPEATS_PER_IMAGE = 3
# 集合 n 小于 30 时，必须附 bootstrap 95% CI（§2.1.6）
BOOTSTRAP_REQUIRED_BELOW_N = 30

# 报告里的派生量统一按 1 位小数记，比对时留 0.15 的舍入余量。
# 超过这个余量就不是舍入，是报告自己写错了。
DERIVED_TOLERANCE_MS = 0.15


# ---------------------------------------------------------------------------
# 汇总统计
# ---------------------------------------------------------------------------


def percentile(
    values: Sequence[float], ratio: float, *, method: str = "linear"
) -> float:
    """百分位数。两种口径，用哪种必须明确。

    2026-11-16 实测修正：A 的 `bench_ocr.py` 用的是**线性插值**，不是最邻近秩。
    同一组数据（五图中位 976.9 / 2690.2 / 1388.2 / 2822.5 / 3162.3）：

    - 线性插值 p95 = **3094.4**（A 报告 `k03_ref_v1_measurement_20261115.json` 里的数）
    - 最邻近秩 p95 = **3162.3**（= 最大值）

    两者差 68 ms（2.2%），足以让同一份报告在两个人手里得出不同结论——这正是本模块
    要防的「一份数据三个判法」。故默认改为 `linear` 与 A 对齐；`nearest_rank` 仅保留
    给确实需要整数秩的场合。`ratio` 传 0.95 即 p95，空序列返回 0.0。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if method == "nearest_rank":
        idx = min(n - 1, max(0, int(round(ratio * (n - 1)))))
        return ordered[idx]
    if method == "linear":
        if n == 1:
            return ordered[0]
        pos = ratio * (n - 1)
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return ordered[int(pos)]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    raise ValueError(f"未知的 percentile 口径：{method!r}")


def _intra_image_median(samples: Sequence[float]) -> float:
    if not samples:
        raise ValueError("每图至少要有一条耗时样本")
    return statistics.median(samples)


def compute_aggregates(
    samples_by_image: dict[str, Sequence[float]],
    *,
    bootstrap_resamples: int = 2000,
    bootstrap_seed: int = 0,
) -> dict[str, Any]:
    """把「每图多次原始耗时」汇总成共识要求的统计量。

    参数 `samples_by_image`：{图 id: [该图每次稳态耗时 ms, ...]}。
    返回聚合结果，字段名与 B 最终意见 §2.1 一一对应。
    """
    image_ids = list(samples_by_image)
    if not image_ids:
        raise ValueError("至少需要一张图")

    image_medians: dict[str, float] = {}
    image_ranges: dict[str, float] = {}
    for image_id, samples in samples_by_image.items():
        if len(samples) < MIN_REPEATS_PER_IMAGE:
            raise ValueError(
                f"图 {image_id} 只有 {len(samples)} 次稳态样本，"
                f"共识要求每图至少 {MIN_REPEATS_PER_IMAGE} 次"
            )
        image_medians[image_id] = _intra_image_median(samples)
        image_ranges[image_id] = max(samples) - min(samples)

    medians = list(image_medians.values())
    n = len(medians)

    aggregates: dict[str, Any] = {
        "median_of_image_medians": round(statistics.median(medians), 1),
        "p95_of_image_medians": round(percentile(medians, 0.95), 1),
        "max_of_image_medians": round(max(medians), 1),
        "n_images": n,
        "median_of_intra_image_ranges": round(
            statistics.median(list(image_ranges.values())), 1
        ),
    }

    # p95 的 n < 30 时另报 bootstrap 95% CI（§2.1.6），不按全部运行重算
    if n < BOOTSTRAP_REQUIRED_BELOW_N:
        rng = random.Random(bootstrap_seed)
        boot: list[float] = []
        for _ in range(bootstrap_resamples):
            resample = [medians[rng.randrange(n)] for _ in range(n)]
            boot.append(percentile(resample, 0.95))
        boot.sort()
        lo = boot[int(0.025 * (len(boot) - 1))]
        hi = boot[int(0.975 * (len(boot) - 1))]
        aggregates["bootstrap_95_ci_p95"] = [round(lo, 1), round(hi, 1)]

    return aggregates


def evaluate_gates(
    aggregates: dict[str, Any],
    *,
    reference_ms: float = T_OCR_REF_V2_MS,
) -> dict[str, Any]:
    """Evaluate the frozen K-03 gates against one aggregate report."""
    if isinstance(reference_ms, bool) or reference_ms <= 0:
        raise ValueError("reference_ms 必须是大于 0 的数值")
    median_ms = _number(aggregates.get("median_of_image_medians"))
    p95_ms = _number(aggregates.get("p95_of_image_medians"))
    max_ms = _number(aggregates.get("max_of_image_medians"))
    if median_ms is None or p95_ms is None or max_ms is None:
        raise ValueError("aggregates 缺少可计算的 median / p95 / max")

    median_limit = reference_ms * GATE_MEDIAN_MULT
    p95_limit = reference_ms * GATE_P95_MULT
    max_explanation_line = reference_ms * GATE_MAX_MULT
    return {
        "data_version": DATA_VERSION,
        "reference_ms": round(reference_ms, 1),
        "median_ms": round(median_ms, 1),
        "median_limit_ms": round(median_limit, 1),
        "median_ratio": round(median_ms / reference_ms, 3),
        "median_passed": median_ms <= median_limit,
        "p95_ms": round(p95_ms, 1),
        "p95_limit_ms": round(p95_limit, 1),
        "p95_ratio": round(p95_ms / reference_ms, 3),
        "p95_passed": p95_ms <= p95_limit,
        "max_ms": round(max_ms, 1),
        "max_explanation_line_ms": round(max_explanation_line, 1),
        "max_ratio": round(max_ms / reference_ms, 3),
        "max_requires_explanation": max_ms > max_explanation_line,
    }


def _print_gates(gates: dict[str, Any]) -> None:
    print(
        f"[gate] 基线 = {gates['reference_ms']:.1f} ms"
        f"（{gates['data_version']}）"
    )
    print(
        f"  median {gates['median_ms']:.1f} / {gates['median_limit_ms']:.1f} ms"
        f" = {gates['median_ratio']:.3f}x → "
        f"{'PASS' if gates['median_passed'] else 'FAIL'}"
    )
    print(
        f"  p95    {gates['p95_ms']:.1f} / {gates['p95_limit_ms']:.1f} ms"
        f" = {gates['p95_ratio']:.3f}x → "
        f"{'PASS' if gates['p95_passed'] else 'FAIL'}"
    )
    max_status = (
        "需逐图解释"
        if gates["max_requires_explanation"]
        else "无需额外解释"
    )
    print(
        f"  max    {gates['max_ms']:.1f} / "
        f"{gates['max_explanation_line_ms']:.1f} ms"
        f" = {gates['max_ratio']:.3f}x → {max_status}"
    )


# ---------------------------------------------------------------------------
# 模板与校验
# ---------------------------------------------------------------------------

# 顶层必需字段（缺任一 → 报告无效）
REQUIRED_TOP_LEVEL: tuple[str, ...] = (
    "data_version",
    "generated_at",
    "cpu",
    "power_state",
    "method",
    "generator_command",
    "generator_seed",
    "reference_set",
    "images",
    "aggregates",
    "pixel_median",
    "ocr_chars_median",
    "t_ocr_ref_v2",
    "cold_start_ms",
)

# method 子对象必需字段
REQUIRED_METHOD: tuple[str, ...] = (
    "process_warmup_runs",
    "per_image_discard_runs",
    "per_image_repeats",
)

# reference_set 每项必需字段（冻结记录，§2.2）
REQUIRED_REF_ENTRY: tuple[str, ...] = (
    "id",
    "width",
    "height",
    "content",
    "drawn_chars",
    "sha256",
    "ocr_chars",
)

# images 每项必需字段（测量结果，§2.1）
REQUIRED_IMAGE_ENTRY: tuple[str, ...] = ("id", "samples_ms", "median_ms", "range_ms")

# aggregates 必需字段
REQUIRED_AGGREGATES: tuple[str, ...] = (
    "median_of_image_medians",
    "p95_of_image_medians",
    "max_of_image_medians",
    "n_images",
    "median_of_intra_image_ranges",
)


def _missing(top: dict[str, Any], fields: Sequence[str]) -> list[str]:
    return [f for f in fields if f not in top]


def _number(value: Any) -> float | None:
    """把 JSON 里的数字取成 float；布尔、字符串、None 一律视为「不是数」。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def validate_report(report: dict[str, Any]) -> list[str]:
    """校验一份 K-03 报告。返回问题列表；空列表 = 通过。

    只查「缺字段 / 口径不一致」这类结构问题，不重算耗时、不判通过失败。
    """
    problems: list[str] = []

    if not isinstance(report, dict):
        return ["报告必须是 JSON 对象"]

    for field in _missing(report, REQUIRED_TOP_LEVEL):
        problems.append(f"缺必需字段：{field}")

    if report.get("data_version") != DATA_VERSION:
        problems.append(
            f"data_version 应为 {DATA_VERSION!r}，实际 {report.get('data_version')!r}"
        )

    method = report.get("method")
    if isinstance(method, dict):
        for field in _missing(method, REQUIRED_METHOD):
            problems.append(f"method 缺必需字段：{field}")
    elif method is not None:
        problems.append("method 必须是对象")

    ref_set = report.get("reference_set")
    if isinstance(ref_set, list):
        if len(ref_set) != len(REFERENCE_SET):
            problems.append(
                f"reference_set 应恰好 {len(REFERENCE_SET)} 张，实际 {len(ref_set)} 张"
            )
        for i, entry in enumerate(ref_set):
            if not isinstance(entry, dict):
                problems.append(f"reference_set[{i}] 不是对象")
                continue
            for field in _missing(entry, REQUIRED_REF_ENTRY):
                problems.append(f"reference_set[{i}] 缺必需字段：{field}")
            if entry.get("sha256"):
                sha = str(entry["sha256"])
                if len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
                    problems.append(f"reference_set[{i}].sha256 不是合法 SHA-256")
    elif ref_set is not None:
        problems.append("reference_set 必须是数组")

    images = report.get("images")
    ref_ids = [
        str(entry["id"])
        for entry in ref_set
        if isinstance(entry, dict) and entry.get("id") is not None
    ] if isinstance(ref_set, list) else []

    if isinstance(images, list):
        if not images:
            problems.append("images 不能为空")
        seen_ids: list[str] = []
        for i, entry in enumerate(images):
            if not isinstance(entry, dict):
                problems.append(f"images[{i}] 不是对象")
                continue
            for field in _missing(entry, REQUIRED_IMAGE_ENTRY):
                problems.append(f"images[{i}] 缺必需字段：{field}")

            image_id = entry.get("id")
            if image_id is not None:
                seen_ids.append(str(image_id))
            label = f"images[{i}]（{image_id!r}）"

            samples = entry.get("samples_ms")
            if not isinstance(samples, list):
                continue
            if len(samples) < MIN_REPEATS_PER_IMAGE:
                problems.append(
                    f"{label}只有 {len(samples)} 次稳态样本，"
                    f"共识要求每图至少 {MIN_REPEATS_PER_IMAGE} 次"
                )
                continue

            values = [_number(v) for v in samples]
            if any(v is None for v in values):
                problems.append(f"{label}samples_ms 含非数值项，无法重算")
                continue
            numbers = [v for v in values if v is not None]
            non_positive = [v for v in numbers if v <= 0]
            if non_positive:
                problems.append(
                    f"{label}samples_ms 出现非正耗时 {non_positive[:3]}；"
                    "稳态耗时必须 > 0，全零说明模板还没填"
                )
                continue

            actual_median = float(statistics.median(numbers))
            actual_range = max(numbers) - min(numbers)
            claimed_median = _number(entry.get("median_ms"))
            claimed_range = _number(entry.get("range_ms"))
            if claimed_median is None:
                problems.append(f"{label}median_ms 不是数")
            elif abs(claimed_median - actual_median) > DERIVED_TOLERANCE_MS:
                problems.append(
                    f"{label}median_ms={claimed_median:.1f} 与 samples 实算中位数 "
                    f"{actual_median:.1f} 不符"
                )
            if claimed_range is None:
                problems.append(f"{label}range_ms 不是数")
            elif abs(claimed_range - actual_range) > DERIVED_TOLERANCE_MS:
                problems.append(
                    f"{label}range_ms={claimed_range:.1f} 与 samples 实算极差 "
                    f"{actual_range:.1f} 不符"
                )

        duplicated = sorted({x for x in seen_ids if seen_ids.count(x) > 1})
        if duplicated:
            problems.append(f"images 中有重复的图 id：{duplicated}")
        if ref_ids:
            missing_ref = [x for x in ref_ids if x not in seen_ids]
            extra = [x for x in seen_ids if x not in ref_ids]
            if missing_ref:
                problems.append(
                    f"images 缺少参考集里的图：{missing_ref}"
                    f"（固定 {len(ref_ids)} 张，不得少测）"
                )
            if extra:
                problems.append(f"images 含参考集之外的图：{extra}")
    elif images is not None:
        problems.append("images 必须是数组")

    aggregates = report.get("aggregates")
    if isinstance(aggregates, dict):
        for field in _missing(aggregates, REQUIRED_AGGREGATES):
            problems.append(f"aggregates 缺必需字段：{field}")
        n = aggregates.get("n_images")
        if isinstance(n, int):
            if isinstance(images, list) and n != len(images):
                problems.append(
                    f"aggregates.n_images={n} 与 images 长度 {len(images)} 不一致"
                )
            if n < BOOTSTRAP_REQUIRED_BELOW_N and "bootstrap_95_ci_p95" not in aggregates:
                problems.append(
                    f"n_images={n} < {BOOTSTRAP_REQUIRED_BELOW_N}，"
                    "必须另报 bootstrap_95_ci_p95（§2.1.6）"
                )
        ref_value = _number(report.get("t_ocr_ref_v2"))
        median_value = _number(aggregates.get("median_of_image_medians"))
        if ref_value is not None and median_value is not None:
            if abs(ref_value - median_value) > DERIVED_TOLERANCE_MS:
                problems.append(
                    f"t_ocr_ref_v2={ref_value:.1f} 与 aggregates.median_of_image_medians="
                    f"{median_value:.1f} 不一致；模板定义二者相等"
                    "（t_ocr_ref_v2 就是五张图图内中位耗时的中位数）"
                )
    elif aggregates is not None:
        problems.append("aggregates 必须是对象")

    # 市电是验收前提（§2.1.7）：电池结果只做诊断，提示但不算结构无效
    power = report.get("power_state")
    if isinstance(power, str) and not power.startswith("AC"):
        problems.append(
            f"power_state={power!r} 非市电；电池结果只能作诊断，不能作为验收结论"
        )

    return problems


# ---------------------------------------------------------------------------
# A 现有产出格式（`tools/make_k03_reference.py`）：结构不同，只能查自洽
# ---------------------------------------------------------------------------

A_FORMAT_REQUIRED: tuple[str, ...] = (
    "version",
    "generated_at",
    "host",
    "config",
    "summary",
    "rows",
)

# 模板要求、但 A 现有报告里根本没有的字段（用于给 A 列缺口，不判错）
TEMPLATE_ONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("images[].samples_ms", "每图原始稳态耗时；没有它无法复算图内中位数与极差"),
    ("images[].range_ms", "图内极差；模板要求的「极差中位数」算不出来"),
    ("reference_set[].sha256", "参考集逐图哈希；只有一张总 manifest 哈希，无法定位单图被换"),
    ("power_state", "模板用 power_state，A 现在写在 host.power"),
    ("method", "模板用 method.*，A 现在写在 config.*"),
    ("generator_command", "参考集生成命令，缺了无法复现"),
    ("generator_seed", "生成种子，缺了无法复现"),
    ("cold_start_ms", "冷启动单列；A 的报告没有这一项"),
    ("pixel_median", "像素量中位数，A 现在叫 summary.median_megapixels"),
)


def is_a_format(report: Any) -> bool:
    """判断是不是 A 的 rows/summary 形态（而非 C 的模板形态）。"""
    return isinstance(report, dict) and "rows" in report and "data_version" not in report


def validate_a_format(report: dict[str, Any]) -> list[str]:
    """校验 A 的 `make_k03_reference.py` 产出的报告。

    与模板口径不同：rows 只存每图中位数，没有 samples_ms / range_ms，
    因此无法重算图内极差——这里只查它**内部是否自洽**，并单独列出模板缺口。
    返回问题列表；空列表 = 自洽（不等于满足模板，缺口看 TEMPLATE_ONLY_FIELDS）。
    """
    problems: list[str] = []
    for field in A_FORMAT_REQUIRED:
        if field not in report:
            problems.append(f"缺必需字段：{field}")
    if report.get("version") != DATA_VERSION:
        problems.append(f"version 应为 {DATA_VERSION!r}，实际 {report.get('version')!r}")

    host = report.get("host") or {}
    power = str(host.get("power") or "").lower()
    if power and not power.startswith("ac"):
        problems.append(
            f"host.power={host.get('power')!r} 非市电；电池结果只能作诊断"
        )

    config = report.get("config") or {}
    runs = config.get("runs_per_image")
    if isinstance(runs, int) and runs < MIN_REPEATS_PER_IMAGE:
        problems.append(
            f"config.runs_per_image={runs} 少于共识要求的 {MIN_REPEATS_PER_IMAGE} 次"
        )

    rows = report.get("rows")
    if not isinstance(rows, list) or not rows:
        problems.append("rows 必须是非空数组")
        return problems

    ids: list[str] = []
    medians: list[float] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"rows[{i}] 不是对象")
            continue
        rid = row.get("id")
        if rid is not None:
            ids.append(str(rid))
        value = _number(row.get("image_median_ms"))
        if value is None:
            problems.append(f"rows[{i}]（{rid!r}）image_median_ms 不是数")
        elif value <= 0:
            problems.append(f"rows[{i}]（{rid!r}）image_median_ms={value} 必须 > 0")
        else:
            medians.append(value)

    expect_ids = [rid for rid, *_ in REFERENCE_SET]
    if ids and sorted(ids) != sorted(expect_ids):
        problems.append(f"rows 的图 id 应为 {expect_ids}，实际 {ids}")

    summary = report.get("summary") or {}
    if medians:
        claimed_med = _number(summary.get("median_of_image_medians_ms"))
        if claimed_med is None:
            problems.append("summary 缺 median_of_image_medians_ms")
        else:
            actual_med = statistics.median(medians)
            if abs(claimed_med - actual_med) > DERIVED_TOLERANCE_MS:
                problems.append(
                    f"summary.median_of_image_medians_ms={claimed_med:.1f} 与 rows 实算 "
                    f"{actual_med:.1f} 不符"
                )

        claimed_p95 = _number(summary.get("p95_of_image_medians_ms"))
        if claimed_p95 is None:
            problems.append("summary 缺 p95_of_image_medians_ms")
        else:
            actual_p95 = percentile(medians, 0.95)
            if abs(claimed_p95 - actual_p95) > DERIVED_TOLERANCE_MS:
                rank_p95 = percentile(medians, 0.95, method="nearest_rank")
                problems.append(
                    f"summary.p95_of_image_medians_ms={claimed_p95:.1f} 与线性插值实算 "
                    f"{actual_p95:.1f} 不符（最邻近秩为 {rank_p95:.1f}，说明两处口径不同）"
                )

        claimed_max = _number(summary.get("max_of_image_medians_ms"))
        if claimed_max is not None and abs(claimed_max - max(medians)) > DERIVED_TOLERANCE_MS:
            problems.append(
                f"summary.max_of_image_medians_ms={claimed_max:.1f} 与 rows 实算 "
                f"{max(medians):.1f} 不符"
            )

    n = summary.get("n_images")
    if isinstance(n, int) and n != len(rows):
        problems.append(f"summary.n_images={n} 与 rows 长度 {len(rows)} 不一致")

    ci = summary.get("p95_bootstrap_ci_ms")
    if isinstance(ci, list) and len(ci) == 2:
        lo, hi = _number(ci[0]), _number(ci[1])
        if lo is not None and hi is not None and lo > hi:
            problems.append(f"p95_bootstrap_ci_ms 下界 {lo} 大于上界 {hi}")

    return problems


def build_template() -> dict[str, Any]:
    """输出一份空模板：字段齐全、数值留空/占位，供 A 照着填。"""
    return {
        "data_version": DATA_VERSION,
        "generated_at": "<ISO-8601，如 2026-11-16T00:00:00+08:00>",
        "cpu": "<CPU 型号，如 Intel i5-12400>",
        "power_state": "AC",  # 必须是 AC；电池只作诊断
        "method": {
            "process_warmup_runs": 1,   # 进程预热 1 次
            "per_image_discard_runs": 1,  # 每图先作废 1 次
            "per_image_repeats": 3,     # 随后至少 3 次
        },
        "generator_command": "<生成参考集的命令，含 seed>",
        "generator_seed": "<seed 值>",
        "reference_set": [
            {
                "id": rid,
                "width": width,
                "height": height,
                "content": content,
                "drawn_chars": drawn_chars,
                "sha256": "<64 位 SHA-256>",
                "ocr_chars": "<OCR 实际识别字符数>",
                "font": "<字体>",
                "font_size": "<字号>",
            }
            for rid, width, height, content, drawn_chars in REFERENCE_SET
        ],
        "images": [
            {
                "id": rid,
                "samples_ms": [0.0, 0.0, 0.0],  # 稳态耗时，≥3 次
                "median_ms": 0.0,               # 图内中位数
                "range_ms": 0.0,                # 图内极差 max-min
            }
            for rid, *_ in REFERENCE_SET
        ],
        "aggregates": {
            "median_of_image_medians": 0.0,
            "p95_of_image_medians": 0.0,
            "max_of_image_medians": 0.0,
            "n_images": len(REFERENCE_SET),
            "median_of_intra_image_ranges": 0.0,
            "bootstrap_95_ci_p95": [0.0, 0.0],  # n<30 必填
        },
        "pixel_median": 0.0,
        "ocr_chars_median": 0.0,
        "t_ocr_ref_v2": 0.0,  # = median(五张图各自的图内中位耗时)
        "cold_start_ms": 0.0,  # 冷启动单列，不进稳态统计
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="K-03 性能报告模板与校验器（三方共识 2026-11-15）",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("template", help="输出空模板（JSON）")

    p_validate = sub.add_parser("validate", help="校验一份报告")
    p_validate.add_argument("report", help="报告 JSON 路径")
    p_validate.add_argument(
        "--ref-ms",
        type=float,
        default=T_OCR_REF_V2_MS,
        help=f"冻结基线（默认 {T_OCR_REF_V2_MS} ms）",
    )

    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(build_template(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "validate":
        path = Path(args.report)
        if not path.is_file():
            print(f"[error] 找不到报告：{path}", file=sys.stderr)
            return 2
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[error] 报告不是合法 JSON：{exc}", file=sys.stderr)
            return 2
        if is_a_format(report):
            print("[format] A 形态（rows / summary，由 make_k03_reference.py 产出）")
            problems = validate_a_format(report)
            gaps = TEMPLATE_ONLY_FIELDS
            summary = report.get("summary") or {}
            gate_input = {
                "median_of_image_medians": summary.get("median_of_image_medians_ms"),
                "p95_of_image_medians": summary.get("p95_of_image_medians_ms"),
                "max_of_image_medians": summary.get("max_of_image_medians_ms"),
            }
        else:
            problems = validate_report(report)
            gaps = ()
            gate_input = report.get("aggregates") or {}
        if problems:
            print(f"[invalid] {len(problems)} 个问题：")
            for problem in problems:
                print(f"  - {problem}")
        if gaps:
            print(f"[gaps] 与 C 模板的字段缺口 {len(gaps)} 项（不计为错误，但算不全模板要求）：")
            for name, why in gaps:
                print(f"  - {name}：{why}")
        if not problems:
            try:
                gates = evaluate_gates(gate_input, reference_ms=args.ref_ms)
            except ValueError as exc:
                print(f"[gate] 无法判定：{exc}")
                return 1
            if abs(args.ref_ms - T_OCR_REF_V2_MS) > 1e-9:
                print(
                    "[gate] WARNING：使用了自定义基线，仅用于诊断；"
                    f"正式验收必须使用 {T_OCR_REF_V2_MS:.1f} ms"
                )
            _print_gates(gates)
        if problems:
            return 1
        if not (gates["median_passed"] and gates["p95_passed"]):
            return 1
        print("[ok] 报告自洽、口径一致")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
