"""检测耗时的可复现测量：把"冷启动"和"稳态"分开报。

## 为什么要这个工具

2026-11-07 C 报「`image-dev-001` 上 T_ocr 5513 ms，是 W4 线的 2.7 倍，
根因是测量对象不同（4.8 倍像素）」。2026-11-08 复核发现**这个结论是错的**：

- 新进程**首次**调用 = **5781 ms**（含 RapidOCR 模型加载与推理图构建）
- 同一进程**第二次** = **1914 ms**
- 单独测纯 OCR 稳态 = **1678 ms**，与 A 的冻结基线 1686.3 ms 只差 0.5%

也就是说 5513 ms 是**冷启动**，不是像素、也不是字符数。A 在 2026-11-08 的
报告里贴过他自己机器上同一现象：`[5518, 2941, 2863, 2795, 2859, 2804]`——
首发的 5518 与 C 的 5513 是同一件事。

**测量方法错一次，能让人花两天去改一个不存在的性能问题。** 所以把它固化成工具：
默认先预热，只报稳态；需要看冷启动时用 `--cold` 明确要求。

## 用法

    python tools/bench_detect.py dataset/dev/images/image-dev-001.jpg
    python tools/bench_detect.py dataset/dev/images --limit 10 --json out.json
    python tools/bench_detect.py <图> --cold          # 另起进程测冷启动，对照用
    python tools/bench_detect.py <图> --repeat 5      # 稳态重复次数

## 口径（与 A 的 bench_ocr 对齐）

- `chars` = 所有 OCR 文本块的字符总数
- 稳态 = 预热之后重复 `--repeat` 次，报 min 与 median
- **不报"平均值"**：OCR 耗时右偏，平均值会被单次抖动拉走
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "app" / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


def resolve_detector() -> tuple[Callable[..., Any], str]:
    """找到可用的 `analyze(path, image_id, kinds=...)`。

    **两种环境都要能跑**，否则这个工具只对 C 自己有用：
    - C / B 的仓库：`photocheck.detectors.a_adapter`（适配层，带契约校验）
    - A 自己的交付包：顶层就是 `core/detect`，没有 photocheck 包

    2026-11-08 第一版只写了前者，发给 A 之后他那边会直接 ImportError。
    """
    try:
        from photocheck.detectors.a_adapter import analyze_a

        return analyze_a, "photocheck.detectors.a_adapter"
    except ImportError:
        pass

    for root in (ROOT, Path.cwd(), ROOT.parent):
        if not (root / "core" / "detect" / "__init__.py").is_file():
            continue
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from core.detect import analyze as core_analyze

        return (
            lambda image, image_id, kinds: core_analyze(image, image_id, kinds=kinds),
            f"{root.name}/core.detect",
        )

    raise SystemExit(
        "找不到检测器。这个工具需要二者之一：\n"
        "  · 本仓库的 app/vendor/photocheck（C/B 环境）\n"
        "  · 或当前目录/上级目录里有 core/detect/（A 的交付包）\n"
        "把本脚本放进 A 的包根目录再跑即可。"
    )


def resolve_ocr() -> Any:
    """找到 `detect_text_blocks`，路径规则同 resolve_detector。"""
    try:
        from photocheck.detectors.a_engine.core.detect import ocr

        return ocr
    except ImportError:
        pass
    from core.detect import ocr

    return ocr


def power_state() -> str:
    """读电源与电池状态。

    A 的实测证明：同一台机器、同一张图，电池供电比接电源慢 73%。
    这是**必须记录**的测量条件，不记就等于不知道自己在测什么。
    """
    class PowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("Reserved1", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    try:
        status = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return "unknown(api failed)"
        ac = {0: "battery", 1: "AC", 255: "unknown"}.get(status.ACLineStatus, "?")
        pct = status.BatteryLifePercent
        pct_text = f"{pct}%" if pct != 255 else "n/a"
        return f"{ac}, battery {pct_text}"
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 不可用时不该让测量失败
        return "unknown"


def image_facts(pil: Any) -> dict[str, Any]:
    blocks = resolve_ocr().detect_text_blocks(pil)
    return {
        "mp": round(pil.size[0] * pil.size[1] / 1e6, 3),
        "blocks": len(blocks),
        "chars": sum(len(b.text) for b in blocks),
    }


def bench(fn: Callable[[], Any], repeat: int) -> dict[str, float]:
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "min_ms": round(min(times), 1),
        "median_ms": round(statistics.median(times), 1),
        "max_ms": round(max(times), 1),
        "runs": len(times),
    }


CASES = (
    ("ocr_only", ("text",), "纯 OCR（与 A 的 T_ocr 同口径）"),
    ("text_qr", ("text", "qr"), "OCR + 二维码"),
    ("text_qr_face", ("text", "qr", "face"), "OCR + 二维码 + 人脸（应用默认）"),
    ("qr_only", ("qr",), "仅二维码"),
    ("face_only", ("face",), "仅人脸"),
)


def measure_one(path: Path, repeat: int) -> dict[str, Any]:
    from PIL import Image

    analyze_a, _source = resolve_detector()
    pil = Image.open(path).convert("RGB")

    if repeat <= 0:
        # --cold：只跑一次，保留模型加载与缓存未命中的成本。
        # **必须先测再算 image_facts**——facts 里要跑 OCR，会顺手把模型加载掉，
        # 冷启动就被悄悄预热了（第一版踩过：报 3024 ms 而不是 5.7 s）。
        t0 = time.perf_counter()
        analyze_a(str(path), path.stem, kinds=("text", "qr", "face"))
        cold_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {**image_facts(pil), "cold_full_ms": cold_ms}

    facts = image_facts(pil)

    # 预热：这一发含模型加载，绝不进统计
    analyze_a(str(path), "warmup", kinds=("text",))

    row: dict[str, Any] = dict(facts)
    for key, kinds, _label in CASES:
        row[key] = bench(lambda k=kinds: analyze_a(str(path), path.stem, kinds=k), repeat)
    ocr = row["ocr_only"]["median_ms"]
    row["ms_per_kchar"] = round(ocr / max(facts["chars"], 1) * 1000, 0)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="检测耗时可复现测量（冷启动/稳态分开）")
    ap.add_argument("target", help="图片路径，或包含 *.jpg 的目录")
    ap.add_argument("--repeat", type=int, default=3, help="稳态重复次数（默认 3）")
    ap.add_argument("--limit", type=int, default=0, help="目录模式下最多测几张")
    ap.add_argument("--cold", action="store_true", help="只跑一次，测冷启动（需每次新进程）")
    ap.add_argument("--json", help="把结果写到这个文件")
    args = ap.parse_args()

    target = Path(args.target)
    files = sorted(target.glob("*.jpg")) if target.is_dir() else [target]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"没找到图片：{target}")
        return 2

    env = {
        "power": power_state(),
        "repeat": 0 if args.cold else args.repeat,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    print(f"测量条件：电源 {env['power']}　重复 {env['repeat'] or '冷启动单发'}")
    print()

    rows = []
    for i, f in enumerate(files, 1):
        row = measure_one(f, 0 if args.cold else args.repeat)
        row["image"] = f.name
        rows.append(row)
        if args.cold:
            print(f"[{i}/{len(files)}] {f.name:22} 冷启动 {row['cold_full_ms']:7.0f} ms"
                  f"  {row['mp']} MP {row['chars']} 字符")
        else:
            print(f"[{i}/{len(files)}] {f.name:22} 纯OCR {row['ocr_only']['median_ms']:7.0f} ms"
                  f"  +QR {row['text_qr']['median_ms']:7.0f}"
                  f"  +人脸 {row['text_qr_face']['median_ms']:7.0f}"
                  f"  {row['mp']} MP {row['chars']} 字符", flush=True)

    if not args.cold and len(rows) > 1:
        med = statistics.median
        print("\n=== 汇总（中位）===")
        for key, _kinds, label in CASES:
            print(f"  {label:28} {med([r[key]['median_ms'] for r in rows]):7.0f} ms")
        print(f"  {'字符数':28} {med([r['chars'] for r in rows]):7.0f}")
        print(f"  {'像素':28} {med([r['mp'] for r in rows]):7.2f} MP")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"env": env, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
