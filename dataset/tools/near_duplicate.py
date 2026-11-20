#!/usr/bin/env python3
"""dev / lock 近重复检测：把"模板不相交"从口头断言变成数字。

为什么需要它：现在 lock 独立于 dev 的依据只有**模板名不同**（invoice/badge/admission vs
receipt/chat/card/notice）。但"名字不同"不等于"图不像"——同一套版式骨架、同样的色块和
字号，换个封面标题就可能长得很像。真要被人问"你凭什么说没泄漏"，回答"名字不一样"是撑不住的。

两个指标：
  - **dHash 汉明距离**（64 位）：0 表示结构几乎一致，≤10 通常算近重复候选；
  - **灰度归一化互相关**（32×32）：连续值，用来在汉明距离打平时排序。

**对照组**比绝对值更有意义：如果 dev 内部最像的一对都有 20，而 dev 与 lock 之间最小也有 25，
那才叫"两套确实不同构"。所以同时算 dev 内部、lock 内部、跨集三种分布。

    python dataset/tools/near_duplicate.py            # 人读摘要 + 最相似若干对
    python dataset/tools/near_duplicate.py --json     # 机器可读，给测试与审计用
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]

# dHash 阈值：≤10/64 通常视为近重复候选（业界常用经验值，不是本项目的硬标准）
NEAR_DUP_MAX = 10
# 只报最相似的若干对，避免一屏几百行没人看
TOP_N = 8


def fingerprint(path: Path) -> tuple[int, list[float]]:
    """(64 位 dHash, 32×32 灰度向量)。只读像素，不依赖 EXIF。"""
    with Image.open(path) as im:
        gray = im.convert("L")

        small = gray.resize((9, 8), Image.LANCZOS)
        pixels = list(small.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)

        grid = list(gray.resize((32, 32), Image.LANCZOS).getdata())
    return bits, [float(v) for v in grid]


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def ncc(a: list[float], b: list[float]) -> float:
    """归一化互相关。1.0 = 完全一致，0 = 无关。"""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    num = sum(x * y for x, y in zip(da, db))
    den = (sum(x * x for x in da) ** 0.5) * (sum(y * y for y in db) ** 0.5)
    return num / den if den else 0.0


def collect(folder: Path) -> dict[str, tuple[int, list[float]]]:
    """读不出来的图直接跳过——数据集里本来就有坏图（非图片 / 截断 JPEG），
    它们是健壮性样本，不参与相似度比较。"""
    out: dict[str, tuple[int, list[float]]] = {}
    for path in sorted((folder / "images").glob("*.jpg")):
        try:
            out[path.stem] = fingerprint(path)
        except (OSError, ValueError):
            continue
    return out


def compare(left: dict, right: dict, cross: bool) -> list[dict]:
    pairs = []
    if cross:
        combos = ((a, b) for a in left for b in right)
    else:
        # 同集内只取无序对，否则每对算两遍
        combos = itertools.combinations(list(left), 2)
    for a, b in combos:
        ha, va = left[a]
        hb, vb = right[b]
        pairs.append({"a": a, "b": b, "hamming": hamming(ha, hb), "ncc": round(ncc(va, vb), 4)})
    return pairs


def summarise(pairs: list[dict]) -> dict:
    if not pairs:
        return {"pairs": 0}
    ham = sorted(p["hamming"] for p in pairs)
    return {
        "pairs": len(pairs),
        "hamming_min": ham[0],
        "hamming_median": ham[len(ham) // 2],
        "hamming_max": ham[-1],
        "ncc_max": max(p["ncc"] for p in pairs),
        "near_dup_le5": sum(1 for h in ham if h <= 5),
        "near_dup_le10": sum(1 for h in ham if h <= NEAR_DUP_MAX),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="dev / lock 近重复检测")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args(argv)

    dev = collect(ROOT / "dataset" / "dev")
    lock = collect(ROOT / "dataset" / "lock")
    if not dev or not lock:
        print("dev 或 lock 为空，先跑 gen_samples.py", file=sys.stderr)
        return 2

    cross = compare(dev, lock, cross=True)
    within_dev = compare(dev, dev, cross=False)
    within_lock = compare(lock, lock, cross=False)

    report = {
        "dev_images": len(dev),
        "lock_images": len(lock),
        "cross": summarise(cross),
        "within_dev": summarise(within_dev),
        "within_lock": summarise(within_lock),
        "threshold": NEAR_DUP_MAX,
        "worst_cross_pairs": sorted(cross, key=lambda p: (p["hamming"], -p["ncc"]))[: args.top],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["cross"]["near_dup_le10"] else 0

    c, d, l = report["cross"], report["within_dev"], report["within_lock"]
    print(f"dev {len(dev)} 张 × lock {len(lock)} 张 = {c['pairs']} 对\n")
    print(f"{'范围':<12}{'最小汉明':>8}{'中位':>6}{'最大':>6}{'最大 NCC':>10}{'≤5':>5}{'≤10':>5}")
    for name, s in (("dev <-> lock", c), ("dev 内部", d), ("lock 内部", l)):
        print(f"{name:<12}{s['hamming_min']:>8}{s['hamming_median']:>6}{s['hamming_max']:>6}"
              f"{s['ncc_max']:>10}{s['near_dup_le5']:>5}{s['near_dup_le10']:>5}")

    print(f"\n最相似的 {args.top} 对（dev <-> lock）：")
    for p in report["worst_cross_pairs"]:
        print(f"  {p['hamming']:>2} 位  NCC {p['ncc']:.3f}  {p['a']} <-> {p['b']}")

    print()
    if c["near_dup_le10"]:
        print(f"[!] 有 {c['near_dup_le10']} 对达到近重复候选（≤{NEAR_DUP_MAX} 位），"
              "锁集独立性存疑，需要人工看图确认")
    else:
        print(f"dev 与 lock 之间没有近重复候选（最近的一对差 {c['hamming_min']} 位）")
    if d["hamming_min"] <= c["hamming_min"]:
        print("注：dev 内部比跨集更相似，符合'同模板更接近、跨模板更远'的预期")
    # 人读模式的退出码要与 --json 一致：否则 CI 里跑人读版永远绿
    return 1 if c["near_dup_le10"] else 0


if __name__ == "__main__":
    sys.exit(main())
