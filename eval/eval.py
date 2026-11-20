#!/usr/bin/env python3
"""评测入口（K-13 · 第一层）。

用法：
    python eval/eval.py --set dev --perfect        # 自测：真值当预测，指标必须是 1.0
    python eval/eval.py --set dev --pred-dir out/  # 用外部预测
    python eval/eval.py --set lock --perfect       # 只在 W8/W20/答辩前跑

结果归档到 `results/<时间戳>-<git短 sha>/`，**不覆盖历史**。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import config, loader, metrics, reporter  # noqa: E402
from eval.matcher import match_image  # noqa: E402

# 契约 v4 的全部 kind。--kinds 只接受这些值，拼错立刻报错而不是静默出 0 分。
EVAL_KINDS = ("text", "qr", "face", "meta")


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def git_dirty() -> bool:
    """工作区是否有未提交改动。结果归档必须能说清"这是哪个版本跑出来的"。"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def allocate_output_dir(base: Path) -> Path:
    """原子创建输出目录；同秒重跑自动加后缀，绝不覆盖上一轮结果。"""
    base.parent.mkdir(parents=True, exist_ok=True)
    for seq in range(1, 1001):
        candidate = base if seq == 1 else base.with_name(f"{base.name}-{seq:02d}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"同名结果目录过多：{base}")


def run(dataset_dir: Path, predictions: dict[str, list[dict]], prediction_label: str,
        difficulties: dict[str, str] | None = None,
        ci_boot: int = 1000, ci_seed: int = 7,
        kinds: Sequence[str] | None = None) -> dict:
    """跑一次评测。

    `kinds` 只评指定类别（如 `("text", "qr")`），默认全部。
    为什么需要它：A 只负责 text/qr/face，meta 是 B 的 K-02。全量评会把 24 条 meta 真值
    全算成漏检，召回从 100% 掉到 64.7%——那不是检测器差，是评测范围没对齐。
    报告必须写明评了哪些 kind，否则数字口径不可比。
    """
    if not dataset_dir.is_dir():
        raise ValueError(f"数据集目录不存在：{dataset_dir}")
    truth_all = loader.load_set(dataset_dir)
    if not truth_all:
        raise ValueError(f"{dataset_dir} 下没有标注文件（labels/*.json）")
    negatives = loader.load_negatives(dataset_dir)
    allow = set(kinds) if kinds else None

    # 覆盖率：预测里一个都没对上真值时，多半是 image_id 拼错了。
    # 这种情况必须报错而不是出 0 分——0 分看起来像"检测器完全失效"，会把人带到错的方向。
    #
    # 口径：**有预测文件就算覆盖**，空数组表示"跑了但什么都没检出"——那正是干净负样本的
    # 正确结果，不是"这张图没跑"。早先用 `if recs` 过滤空数组，5 张干净图被误报成未覆盖。
    pred_images = set(predictions)
    matched = pred_images & truth_all.keys()
    foreign = sorted(pred_images - truth_all.keys())
    if pred_images and not matched:
        sample = "、".join(foreign[:3]) or "（空）"
        raise ValueError(
            f"预测的 image_id 与数据集完全没有交集（预测 {len(foreign)} 张，例如 {sample}）。\n"
            "  评测按 image_id 对齐，对不上就会全部算成漏检。先确认 image_id 拼写与数据集一致。"
        )

    def _scope(records: list[dict]) -> list[dict]:
        if allow is None:
            return records
        return [r for r in records if r.get("kind") in allow]

    def match_all(thresholds=None):
        out = []
        for image_id, truth_records in truth_all.items():
            truth_dets = _scope(loader.detections(truth_records))
            pred_dets = _scope(loader.detections(predictions.get(image_id, [])))
            out.append(match_image(image_id, truth_dets, pred_dets,
                                   negatives.get(image_id, []), thresholds))
        return out

    results = match_all()

    summary = metrics.summarize(results)
    summary["failing_images"] = metrics.failing_images(results)
    summary["by_difficulty"] = metrics.by_difficulty(results, difficulties or {})
    summary["coverage"] = {
        "truth_images": len(truth_all),
        "prediction_images": len(pred_images),
        "matched_images": len(matched),
        "foreign_images": foreign[:10],
        "foreign_count": len(foreign),
        "uncovered_images": len(truth_all) - len(matched),
    }
    # 敏感性分析：只放宽文本类，主表口径一个字不动
    loose = metrics.summarize(match_all({"text": config.LOOSE_IOU_TEXT}))
    summary["loose"] = {
        "threshold": config.LOOSE_IOU_TEXT,
        "recall": loose["recall"], "precision": loose["precision"],
        "tp": loose["tp"], "fn": loose["fn"], "fp": loose["fp"],
        "subtype_errors": loose["subtype_errors"],
    }
    summary["ci"] = metrics.bootstrap_ci(results, ci_boot, ci_seed)
    failures = [f for r in results for f in r.failures]

    context = {
        "dataset": dataset_dir.name,
        "prediction": prediction_label,
        "iou_threshold": config.IOU_THRESHOLD,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "negative_regions": sum(len(v) for v in negatives.values()),
        "difficulty_source": "manifest.csv" if difficulties else "无（只出总表）",
        "ci_boot": ci_boot,
        "kinds": sorted(allow) if allow else "all",
    }
    return {"summary": summary, "context": context, "failures": failures}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PhotoCheck 评测（第一层）")
    ap.add_argument("--set", dest="set_name", choices=["dev", "lock"], default="dev")
    ap.add_argument("--data", default=None, help="数据集目录，默认 dataset/<set>")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--perfect", action="store_true", help="用真值当预测（自测）")
    group.add_argument("--pred-dir", help="预测目录（每图一个 json）")
    group.add_argument("--pred", help="预测文件（json 数组）")
    ap.add_argument("--out", default=None, help="输出目录，默认 results/<时间戳>-<sha>/")
    ap.add_argument("--ci", type=int, default=1000,
                    help="bootstrap 重采样次数（0 = 不算置信区间）")
    ap.add_argument("--seed", type=int, default=7, help="bootstrap 随机种子，保证可复现")
    ap.add_argument("--layer2", default="",
                    help="第二层结果 JSON（tools/run_layer2.py 产出）")
    ap.add_argument("--layer3", default="",
                    help="第三层人工判读 JSON；未填则保持未接入")
    ap.add_argument("--kinds", default="",
                    help="只评这些 kind（逗号分隔，如 text,qr）；默认全部。"
                         "评 A 的检测器时要排除 meta（meta 是 B 的 K-02）")
    args = ap.parse_args(argv)

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] or None
    if kinds:
        unknown = set(kinds) - set(EVAL_KINDS)
        if unknown:
            print(f"[error] 未知 kind：{sorted(unknown)}，允许 {sorted(EVAL_KINDS)}", file=sys.stderr)
            return 2

    dataset_dir = Path(args.data) if args.data else ROOT / "dataset" / args.set_name

    if args.perfect:
        predictions = loader.perfect_predictions(loader.load_set(dataset_dir))
        label = "真值当预测（自测）"
    else:
        src = Path(args.pred_dir or args.pred)
        if not src.exists():
            print(f"预测路径不存在：{src}", file=sys.stderr)
            return 2
        try:
            predictions = loader.load_predictions(src)
        except (ValueError, OSError) as exc:
            # 预测不合契约是**格式问题**，不是检测失败。在这里拦住，
            # 免得指标掉到 0 之后两个人去查算法。
            print(str(exc), file=sys.stderr)
            return 2
        label = str(src)

    if args.out:
        out_dir = Path(args.out)
    else:
        base = ROOT / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{git_sha()}"
        out_dir = allocate_output_dir(base)

    try:
        outcome = run(dataset_dir, predictions, label,
                      difficulties=loader.load_manifest(dataset_dir),
                      ci_boot=args.ci, ci_seed=args.seed, kinds=kinds)
    except ValueError as exc:
        # 数据集缺失、image_id 全对不上：都是**输入问题**，不是检测效果
        print(str(exc), file=sys.stderr)
        return 2
    summary = outcome["summary"]
    try:
        layer2 = (metrics.load_layer(args.layer2, "第二层") if args.layer2
                  else metrics.layer2_placeholder())
        layer3 = (metrics.load_layer(args.layer3, "第三层") if args.layer3
                  else metrics.layer3_placeholder())
    except (ValueError, OSError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    paths = reporter.write_reports(
        out_dir, summary, layer2, layer3,
        outcome["context"], failures=outcome["failures"],
    )

    print(f"数据集 {dataset_dir.name}：{summary['images']} 张图")
    print(f"  真值 {summary['objects_truth']} 个对象 / 预测 {summary['objects_pred']} 个")
    print(f"  TP {summary['tp']}  FN {summary['fn']}  FP {summary['fp']}  "
          f"类别判错 {summary['subtype_errors']}")
    print(f"  召回 {summary['recall']}　精确 {summary['precision']}　"
          f"每图误报 {summary['fp_per_image']}")
    # 掩码正确率与重复检出：老三项指标看不见的两类问题（见 matcher 注释）
    if summary.get("mask_checked"):
        print(f"  掩码正确率 {summary['mask_accuracy']}"
              f"（{summary['mask_checked'] - summary['mask_mismatches']}/{summary['mask_checked']}）")
    if summary.get("duplicates"):
        print(f"  [!] 重复检出 {summary['duplicates']} 条"
              f"（占预测 {summary['duplicate_rate']}）")
    cov = summary["coverage"]
    if cov["uncovered_images"] or cov["foreign_count"]:
        print(f"  [!] 覆盖 {cov['matched_images']}/{cov['truth_images']} 张"
              f"（未覆盖 {cov['uncovered_images']}，对不上真值的预测 {cov['foreign_count']} 张）")
    if summary["small_sample"]:
        print(f"  [!] 样本不足 {config.MIN_N_FOR_CONCLUSION}，{config.SMALL_SAMPLE_NOTE}")
    print(f"结果已写入 {out_dir}")

    if args.perfect:
        ok = (summary["recall"] == 1.0 and summary["precision"] == 1.0
              and summary["fn"] == 0 and summary["fp"] == 0 and summary["subtype_errors"] == 0)
        if not ok:
            print("[FAIL] 自测失败：真值当预测时指标必须是 1.0，说明匹配或统计有 bug")
            return 1
        print("[OK] 自测通过：完美预测下指标全为 1.0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
