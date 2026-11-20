"""读真值、读预测、造「完美预测」。

`perfect_predictions` 是这个模块最重要的东西：**拿真值当预测跑一遍，所有指标必须是 1.0**。
不是 1.0 就说明评测代码自己写错了——这是 A 的检测器还没出来时唯一能验证评测的办法。

**预测必须过契约校验**。不校验的话，A 的输出只要有一个字段名写错（`imageId` 而不是
`image_id`），就会被当成"这几张图什么都没检出"，指标直接掉到 0，而那其实是格式问题。
这种误判最贵：两个人会花一整天查算法，最后发现是拼写。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contract.validate_records import validate_records  # noqa: E402


def load_set(dataset_dir: Path) -> dict[str, list[dict]]:
    """返回 {image_id: 契约 v4 记录列表}。没有标注的图（坏图）不会出现在这里。"""
    records: dict[str, list[dict]] = {}
    for path in sorted((dataset_dir / "labels").glob("*.json")):
        records[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return records


def load_manifest(dataset_dir: Path) -> dict[str, str]:
    """{image_id: difficulty}。难度只记在台账（manifest.csv）里，不在契约记录里。"""
    path = dataset_dir / "manifest.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {row.get("image_id", ""): (row.get("difficulty") or "")
                for row in csv.DictReader(fh)}


def load_negatives(dataset_dir: Path) -> dict[str, list[dict]]:
    path = dataset_dir / "negatives.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _read_json(path: Path):
    """读 JSON，出错时说清是哪个文件——裸的 JSONDecodeError 不给路径，40 个文件里没法找。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列，"
                         f"{exc.msg}") from exc


def _check(label: str, records: list[dict]) -> None:
    """预测必须能过契约。不合格就**当场报错**，不要让它变成"全漏检"。"""
    report = validate_records(records)
    if report.ok:
        return
    first = "；".join(str(i) for i in report.errors[:3])
    more = f"（共 {len(report.errors)} 处，只显示前 3 条）" if len(report.errors) > 3 else ""
    raise ValueError(
        f"预测不合契约 v4：{label}\n  {first}{more}\n"
        "  先跑 contract/validate_records.py 定位，再重跑评测——"
        "格式问题不该被算成检测失败。"
    )


def load_predictions(path: Path, *, validate: bool = True) -> dict[str, list[dict]]:
    """支持两种形态：目录（每图一个 json）或单个 json（数组 / {records:[...]}）。

    `validate=True` 时逐文件过契约校验；只为调试畸形数据时才关掉。

    **分组键取记录里的 image_id，并对文件名做一致性检查**。以前用文件名当键，
    文件名写成 `foo.json` 而记录里是 `image-dev-001` 时，两边对不上，全部真值都变成漏检、
    全部预测都变成误报，指标 0 分——而脚本正常退出、没有任何提示。格式问题必须在入口拦住。
    """
    if path.is_dir():
        grouped: dict[str, list[dict]] = {}
        for p in sorted(path.glob("*.json")):
            records = _read_json(p)
            if not isinstance(records, list):
                raise ValueError(f"{p.name} 不是记录数组")
            if validate:
                _check(p.name, records)
            image_ids = {str(r.get("image_id", "")) for r in records}
            if len(image_ids) > 1:
                raise ValueError(
                    f"{p.name} 混了多张图的记录：{sorted(image_ids)}；一图一文件，别合并"
                )
            if image_ids == {""}:
                raise ValueError(f"{p.name} 里的记录没有 image_id")
            image_id = image_ids.pop() if image_ids else p.stem
            if image_id != p.stem:
                raise ValueError(
                    f"{p.name} 的文件名与记录里的 image_id 不一致：文件 {p.stem!r} / 记录 {image_id!r}。\n"
                    "  评测按 image_id 与真值对齐，两者不一致会让整批预测落空（看起来像召回 0）。\n"
                    f"  改法：把文件重命名为 {image_id}.json，或修正记录里的 image_id。"
                )
            if image_id in grouped:
                raise ValueError(f"多份文件都声称是 {image_id}：{p.name}")
            grouped[image_id] = records
        return grouped

    data = _read_json(path)
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, list):
        raise ValueError(f"{path} 不是记录数组")
    if validate:
        _check(path.name, data)

    grouped = {}
    for rec in data:
        grouped.setdefault(str(rec.get("image_id", "")), []).append(rec)
    if "" in grouped:
        raise ValueError(
            f"{path} 里有 {len(grouped[''])} 条预测没有 image_id，无法归属到任何一张图"
        )
    return grouped


def perfect_predictions(truth: Mapping[str, list[dict]]) -> dict[str, list[dict]]:
    """真值当预测。任何指标不是 1.0 都说明匹配或统计有错。"""
    return {image_id: [dict(r) for r in records] for image_id, records in truth.items()}


def detections(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("record_type") == "Detection"]
