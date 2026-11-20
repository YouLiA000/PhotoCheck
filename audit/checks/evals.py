"""交付物检查：评测脚本、基线表、A 与 B 的模块。

分工卡里 K-13（C）、K-08（B）、K-05（A）是三张不同的卡。这里的检查分别标了
owner，报告末尾的「按归属」才能说清「差的是谁的活」，而不是笼统说项目没做完。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

from audit.model import Context, Result, check, fail, ok, partial, skip


@check("EV-01", "K-13", "评测脚本 eval.py 存在且能跑 --help", level="P0", owner="C", gate=("W8",))
def eval_script(ctx: Context) -> Result:
    # 搜根目录 + eval/ tools/ src/。/eval/ 是 K-13 评测脚本的正式位置，
    # 早前只搜了 tools/ 与 src/，把已存在的 eval/eval.py 误报成「找不到」——漏报比误报更危险。
    cands = sorted(ctx.root.glob("eval*.py"))
    for base in (ctx.root / "eval", ctx.root / "tools", ctx.root / "src"):
        if base.is_dir():
            cands += sorted(base.glob("eval*.py"))
    if not cands:
        return fail("找不到 eval.py",
                    "K-13 主卡：吃 predictions.json + labels/，算检测召回/误报、脱敏残留、过度脱敏三层")
    import subprocess
    import sys
    from pathlib import Path

    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    proc = subprocess.run([str(python), str(cands[0]), "--help"], cwd=str(ctx.root),
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return fail(f"{cands[0].name} --help 失败", (proc.stderr or "").strip()[:120])
    return ok(f"{cands[0].relative_to(ctx.root).as_posix()} 可执行")


@check("EV-02", "K-13", "三层评测表已产出，每指标带 n", level="P0", owner="C", gate=("W8",))
def baseline_tables(ctx: Context) -> Result:
    result_dirs = []
    for base in (ctx.root / "reports", ctx.root / "results"):
        if not base.is_dir():
            continue
        result_dirs.extend(
            path.parent
            for path in base.rglob("summary.json")
            if path.is_file()
        )
    if not result_dirs:
        return skip("reports/ 与 results/ 都没有产物", "W8 闸门要求第一版基线表，等 eval.py 跑通后产出")

    rejected: list[str] = []
    for directory in sorted(result_dirs, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(ctx.read_text(directory / "summary.json"))
        except Exception as exc:  # noqa: BLE001
            rejected.append(f"{directory.name}: summary 不可读 ({type(exc).__name__})")
            continue

        layer1 = data.get("layer1") or {}
        if int(layer1.get("objects_truth") or 0) <= 0:
            rejected.append(f"{directory.name}: layer1 没有有效真值对象")
            continue
        rows = _read_csv(directory / "detection_quality.csv")
        if not rows or any(not str(row.get("n") or "").isdigit() for row in rows):
            rejected.append(f"{directory.name}: detection_quality.csv 缺有效 n")
            continue

        layer2 = data.get("layer2") or {}
        layer3 = data.get("layer3") or {}
        if not _completed_layer(layer2):
            rejected.append(f"{directory.name}: layer2 仍是空表 / 未接入")
            continue
        if not _completed_layer(layer3):
            rejected.append(f"{directory.name}: layer3 仍是空表 / 未接入")
            continue

        small = _small_sample_rows([directory / "README.md"])
        return ok(
            f"{directory.name}: 三层均有有效数字，layer1 n={layer1['objects_truth']}"
            + (f"，{small} 行小样本已标注" if small else "")
        )

    return fail(
        "没有一份结果同时包含有效 layer1 / layer2 / layer3 数字",
        "；".join(rejected[:3]) or "重跑 eval.py 并接入最终导出文件与人工判读",
    )


def _read_csv(path) -> list[dict]:
    import csv

    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return []


def _completed_layer(layer: dict) -> bool:
    status = str(layer.get("status") or "").strip().lower()
    if not status or status in {"未接入", "not_connected", "pending", "placeholder"}:
        return False
    return _has_positive_number(layer.get("items") or layer)


def _has_positive_number(value) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, dict):
        return any(_has_positive_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_positive_number(item) for item in value)
    return False


def _small_sample_rows(reports: list) -> int:
    """数一数报告目录里有多少行 n<30（读同名 CSV 的 n 列）。"""
    import csv

    total = 0
    for report in reports:
        for csv_path in report.parent.glob("*.csv"):
            try:
                with open(csv_path, encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        raw = (row.get("n") or "").strip()
                        if raw.isdigit() and int(raw) < 30:
                            total += 1
            except Exception:
                continue
    return total


def _resolve_manifest_entry(entry_name: str):
    """按 B 的 `photocheck/interfaces.json` 解析一个入口；未配置仓库则返回 (None, "")。

    **不要**用 `getattr(importlib.import_module("photocheck"), name)`：
    `photocheck/__init__.py` 没有在包级别重导出这些函数（实测两者都是 False），
    必须从 `entries.<name>.module` 导模块、再取 `.function`。这是 B 在
    `C对接答复` §2.4 指出的，C 原来的伪代码照抄会全部误判。
    """
    repo = os.environ.get("PHOTOCHECK_REPO_ROOT")
    if not repo:
        return None, ""
    root = Path(repo).resolve()
    manifest_path = root / "photocheck" / "interfaces.json"
    if not manifest_path.is_file():
        return None, f"manifest 不存在：{manifest_path.as_posix()}"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["entries"][entry_name]
        module_name, func_name = entry["module"], entry["function"]
    except Exception as exc:  # noqa: BLE001
        return None, f"manifest 解析失败：{type(exc).__name__}: {exc}"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        func = getattr(importlib.import_module(module_name), func_name)
    except Exception as exc:  # noqa: BLE001
        return None, f"导入 {module_name}.{func_name} 失败：{type(exc).__name__}: {exc}"
    if not callable(func):
        return None, f"{module_name}.{func_name} 不可调用"
    return func, f"{module_name}.{func_name} 可调用"


@check("RD-01", "K-08", "C 侧处置输入合契约（Decision 能直接喂给 B）",
       level="P0", owner="C", gate=("W6",))
def redact_ready(ctx: Context) -> Result:
    """验 C 交出去的东西，不验 B 的实现。

    原文查 `src/` 下有没有 `def redact(` —— 那是把 B 的 K-08 算到 C 的闸门上。
    W6 的验收物是"能演示处置"，而 B 的 `apply_redactions()` 在他自己仓库里，
    C 的仓库本来就不该有它。查一个不该存在的文件，结论只能是永远 FAIL。

    C 能保证、也必须保证的是：产出的 Decision 合契约、每个非 meta Detection 恰好一条、
    交接包结构完整——B 拿到就能直接构造 RedactionPlan。
    """
    sys.path.insert(0, str(ctx.root))
    try:
        from ui import decision
    except Exception as exc:  # noqa: BLE001
        return fail(f"ui.decision 导入失败：{type(exc).__name__}", "K-11 界面的决策模块")

    version = "photocheck/v4"
    findings = [
        {"contract_version": version, "record_type": "Detection", "id": "det-t-01",
         "image_id": "img-audit", "kind": "text", "subtype": "phone", "bbox": [10, 10, 100, 30],
         "detector_score": 0.9, "evidence": "ocr_rule", "risk": 2,
         "details": {"masked_preview": "138****0000"}},
        {"contract_version": version, "record_type": "Detection", "id": "det-q-01",
         "image_id": "img-audit", "kind": "qr", "subtype": "qr_link", "bbox": [10, 50, 80, 80],
         "detector_score": None, "evidence": "qr_payload", "risk": 1,
         "details": {"decodable": True, "safe_domain": "example.com"}},
        {"contract_version": version, "record_type": "Detection", "id": "det-m-01",
         "image_id": "img-audit", "kind": "meta", "subtype": "gps", "bbox": None,
         "detector_score": None, "evidence": "exif", "risk": 3, "details": {}},
    ]
    try:
        decisions = decision.build_decisions(findings, ["det-t-01"])
    except Exception as exc:  # noqa: BLE001
        return fail(f"build_decisions 抛错：{type(exc).__name__}", str(exc)[:120])

    # 1. 每个非 meta 恰好一条，meta 一条都不该有
    got = sorted(d["finding_id"] for d in decisions)
    if got != ["det-q-01", "det-t-01"]:
        return fail(f"Decision 覆盖 {got}", "每个非 meta Detection 恰好一条；meta 不产 Decision")

    # 2. 五件必填都在（Decision 契约只有这 5 个字段）
    need = {"contract_version", "record_type", "finding_id", "action", "revision"}
    for d in decisions:
        missing = need - set(d)
        if missing:
            return fail(f"{d.get('finding_id')} 缺字段 {sorted(missing)}", "按契约补齐")
        if d["action"] not in ("redact", "retain"):
            return fail(f"action={d['action']}", "只能是 redact / retain")
        if int(d["revision"]) < 1:
            return fail(f"revision={d['revision']}", "从 1 起")

    # 3. 交接包结构：B 要能一眼分清"契约记录"与"补框素材"
    payload = decision.export_payload(
        "img-audit", decisions, [{"bbox": [1, 2, 3, 4], "method": "solid"}]
    )
    for key in ("contract_version", "image_id", "decisions", "manual_boxes"):
        if key not in payload:
            return fail(f"交接包缺 {key}", "export_payload 的输出结构")

    # 可选：配了 PHOTOCHECK_REPO_ROOT 就顺带验 B 的处置入口可调用
    _func, cross = _resolve_manifest_entry("redact")
    if cross.startswith(("manifest", "导入", "entries")) or "不可调用" in cross:
        return fail(cross, "按 photocheck/interfaces.json 的 entries.redact 解析")
    suffix = f"；跨仓库：{cross}" if cross else "；未设 PHOTOCHECK_REPO_ROOT，跳过跨仓库检查"
    return ok(f"{len(decisions)} 条 Decision 合契约，meta 已排除，交接包结构完整{suffix}")


@check("DET-01", "K-05", "评测链路消费过外部检测器输出（基线可复现）",
       level="P0", owner="C", gate=("W8",))
def detector_ready(ctx: Context) -> Result:
    """验"C 的评测能接住外部检测器"，不是"检测器代码在 C 仓库里"。

    原文查 `src/` 里有没有文件名含 "detect" 的模块——两头都不对：一是 A 的实现不在
    C 的仓库里；二是"文件名含 detect"什么都证明不了，随便建个 `detect_utils.py` 就能骗过去。

    C 对 K-13 的责任是**评测链路**：能读外部预测、能按 image_id 对齐、能算出指标并归档。
    判据改成"存在一份消费外部预测的归档基线"，这是可复核的证据，且完全在 C 的控制范围内。
    """
    results = ctx.root / "results"
    if not results.is_dir():
        return partial("results/ 不存在", "跑一次 eval.py --perfect 自测")
    inspected = 0
    for d in sorted((p for p in results.iterdir() if p.is_dir()), reverse=True):
        summary = d / "summary.json"
        if not summary.exists():
            continue
        try:
            data = json.loads(ctx.read_text(summary))
        except Exception:  # noqa: BLE001
            continue
        inspected += 1
        pred = str((data.get("context") or {}).get("prediction") or "")
        # 自测（真值当预测）证明不了能接真实检测器
        if not pred or "真值" in pred or "perfect" in pred.lower():
            continue
        layer1 = data.get("layer1") or {}
        coverage = layer1.get("coverage") or {}
        if not layer1.get("objects_truth") or not coverage.get("matched_images"):
            continue
        recall = float(layer1.get("recall") or 0.0)
        precision = float(layer1.get("precision") or 0.0)
        # 可选：配了 PHOTOCHECK_REPO_ROOT 就顺带验 A 的检测入口可调用
        _func, cross = _resolve_manifest_entry("detect")
        if cross.startswith(("manifest", "导入", "entries")) or "不可调用" in cross:
            return fail(cross, "按 photocheck/interfaces.json 的 entries.detect 解析")
        suffix = f"；跨仓库：{cross}" if cross else ""
        return ok(f"{d.name}：对齐 {coverage['matched_images']} 张，"
                  f"召回 {recall:.1%} / 精确 {precision:.1%}{suffix}")
    if inspected == 0:
        return partial("results/ 里没有 summary.json", "跑一次 eval.py --perfect 自测")
    return partial("只有自测归档，没消费过外部预测",
                   "eval.py --set dev --pred-dir <A 的输出> --kinds text,qr 跑一次")


@check("EX-01", "K-14", "离线可运行：无网络调用", level="P1", owner="C", gate=("W8",))
def offline_only(ctx: Context) -> Result:
    suspicious = []
    for base in (ctx.src_dir, ctx.ui_dir, ctx.contract_dir):
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            low = ctx.read_text(p).lower()
            for pat in ("requests.get", "requests.post", "urllib.request", "http://api", "https://api"):
                if pat in low:
                    suspicious.append(f"{p.name}:{pat}")
    if suspicious:
        return fail(f"{len(suspicious)} 处疑似联网：{suspicious[:3]}", "K-14 要求离线可运行")
    return ok("src/ ui/ contract/ 里无联网调用")


@check("K03-01", "K-03", "OCR 固定参考集已冻结并按每图中位数测量", level="P0", owner="A", gate=("W8",))
def k03_reference(ctx: Context) -> Result:
    reference_root = ctx.root / "dataset" / "k03_ref_v1"
    manifest_path = reference_root / "manifest.json"
    report_path = ctx.root / "reports" / "k03_ref_v1_measurement_20261115.json"
    if not manifest_path.is_file() or not report_path.is_file():
        return fail("缺少 k03_ref_v1 清单或测量报告", "生成固定参考集并运行 tools/bench_reference.py")
    try:
        import hashlib

        manifest = json.loads(ctx.read_text(manifest_path))
        report = json.loads(ctx.read_text(report_path))
        items = manifest["items"]
        if len(items) != 5:
            return fail(f"参考集有 {len(items)} 张，应为 5 张", "冻结 k03_ref_v1")
        for item in items:
            path = reference_root / item["file"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                return fail(f"{item['id']} 哈希变化", "参考集已冻结，变更需升版本")
        summary = report["summary"]
        n_images = int(summary["n_images"])
        if n_images != 5:
            return fail(f"报告 n_images={n_images}", "按五张参考图完整测量")
        if report.get("host", {}).get("power") != "ac":
            return fail("测量不是 AC 电源", "接市电重测")
        values = [
            float(summary["median_of_image_medians_ms"]),
            float(summary["p95_of_image_medians_ms"]),
            float(summary["max_of_image_medians_ms"]),
        ]
        if min(values) <= 0:
            return fail(f"耗时字段无效：{values}", "重跑 OCR 测量")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        return fail(f"K-03 参考报告不可用：{type(exc).__name__}", "按最终口径重跑")
    return ok(
        f"n={n_images}，median={values[0]:.1f} ms，p95={values[1]:.1f} ms，"
        f"max={values[2]:.1f} ms，AC"
    )
