"""用户决定（Decision）的生成与手动补框（K-11 · W6/W7）。

**为什么单独一个模块**：Decision 是契约 v4 的五类记录之一，不是界面状态。
它要能被 B 的 `redact(image, plan)` 直接吃、能被 `validate_records` 校验、能在报告里
被引用。把这段逻辑埋进 Gradio 回调里，就只能靠点鼠标验证，没法单测。

三条契约规矩（§0.2）：
  1. 只有 `redact` / `retain` 两种动作，**没有 skip / selected / default_action**；
  2. 每个 Finding **最多一个当前决定**——同一 finding 不能出现两条；
  3. 任何决定变化都**递增 `revision`**，旧复检报告随即失效。
  另有一条：**元数据不接受 Decision**（系统始终清除整张图的元数据），
  所以 meta 类发现项不进候选清单。

手动补框不走 Decision：它是一个 `finding_id=null` 的计划区域，
用来补检测漏掉的区域（K-11 W7 验收、K-07 降级路径）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping, Sequence

CONTRACT_VERSION = "photocheck/v4"

# 元数据不参与用户决定：整图元数据始终清除，Finding 只用于告知"发现了哪类"
NON_DECIDABLE_KINDS = frozenset({"meta"})

ACTION_REDACT = "redact"
ACTION_RETAIN = "retain"
ACTIONS = (ACTION_REDACT, ACTION_RETAIN)

DEFAULT_METHOD = "solid"
METHODS = ("solid", "blur", "mosaic")


def eligible(findings: Iterable[Mapping]) -> list[Mapping]:
    """可以参与决定的发现项。meta 排除在外（契约规定不接受 Decision）。"""
    return [f for f in findings if str(f.get("kind")) not in NON_DECIDABLE_KINDS]


def choice_label(finding: Mapping, preview_of) -> tuple[str, str]:
    """CheckboxGroup 的 (显示文本, 值)。显示文本**只能用掩码**（§0.5 红线 1）。"""
    fid = str(finding.get("id"))
    text = f"{fid} ｜ {finding.get('subtype')} ｜ 分值 {finding.get('risk')}"
    preview = preview_of(finding)
    if preview and preview != "—":
        text += f" ｜ {preview}"
    return text, fid


def build_decisions(
    findings: Iterable[Mapping],
    selected_ids: Sequence[str],
    previous: Mapping[str, Mapping] | None = None,
) -> list[dict]:
    """按当前勾选状态生成 Decision 列表。

    - 勾选 = `redact`，取消 = `retain`（契约里没有"不选"这种状态）；
    - **`revision` 是整版计划的版本号，不是单条记录的修改次数**：任一 finding 的动作
      变了（或 finding 集合本身变了），所有当前 Decision **一起**升到同一个新版本，
      没变的项也跟着升。
    - 为什么不能只升变化项：B 的 `validate_records` 要求同一份交接包里所有 Decision
      共享一个 revision，混合版本会被直接拒（2026-11-07 B 核实指出）。语义上也是对的——
      revision 描述的是"这是第几版计划"，B 拿它构造 RedactionPlan、对齐复检报告；
      只给单项 +1 会让 B 不知道该信哪个版本。

    `previous` 传 {finding_id: 上一条 Decision}。传空即"第一次生成"，revision = 1。
    """
    prev = previous or {}
    chosen = {str(x) for x in selected_ids}
    items = eligible(findings)
    targets = {
        str(f["id"]): (ACTION_REDACT if str(f["id"]) in chosen else ACTION_RETAIN)
        for f in items
    }

    old_rev = max((int(p.get("revision") or 0) for p in prev.values()), default=0)
    changed = set(targets) != set(prev) or any(
        prev.get(fid, {}).get("action") != action for fid, action in targets.items()
    )
    revision = 1 if not prev else (old_rev + 1 if changed else old_rev)

    return [
        {
            "contract_version": CONTRACT_VERSION,
            "record_type": "Decision",
            "finding_id": fid,
            "action": action,
            "revision": revision,
        }
        for fid, action in targets.items()
    ]


def index_decisions(decisions: Iterable[Mapping]) -> dict[str, Mapping]:
    """{finding_id: Decision}。同一 finding 出现两条就抛错——契约要求最多一条。"""
    out: dict[str, Mapping] = {}
    for rec in decisions:
        fid = str(rec.get("finding_id"))
        if fid in out:
            raise ValueError(f"同一 finding 出现两条 Decision：{fid}（契约只允许一条当前决定）")
        out[fid] = rec
    return out


def filter_for_findings(
    decisions: Iterable[Mapping], findings: Iterable[Mapping]
) -> tuple[list[Mapping], int]:
    """只保留属于**这一张图**的决定，返回 (保留的, 丢掉的数量)。

    为什么需要：界面的决定存在 State 里，换样本时若没清干净（或脚本直接调），
    导出就会把 A 图的决定挂到 B 图的 image_id 上。B 拿到会照着 B 的尺寸去涂 A 的框，
    而 payload 本身完全合法——**错误数据比报错难发现得多**。
    """
    allowed = {str(f.get("id")) for f in eligible(findings)}
    rows = list(decisions)
    kept = [d for d in rows if str(d.get("finding_id")) in allowed]
    return kept, len(rows) - len(kept)


def summarize(decisions: Iterable[Mapping]) -> dict:
    """给界面看的计数：脱敏几项、保留几项、最大 revision。"""
    recs = list(decisions)
    redact = sum(1 for r in recs if r.get("action") == ACTION_REDACT)
    retain = sum(1 for r in recs if r.get("action") == ACTION_RETAIN)
    return {
        "total": len(recs),
        "redact": redact,
        "retain": retain,
        "max_revision": max((int(r.get("revision") or 0) for r in recs), default=0),
    }


def clamp_bbox(x, y, w, h, *, width: int, height: int) -> list[int]:
    """把手填坐标钳进图内。越界的框如果原样交给处置器，会切出负数索引或越界数组。

    起点在负方向越界时，**同时缩短宽高**，保留用户实际框住的那部分：从 -30 起、宽 500 的框，
    在图上其实只覆盖 0..470。若只把 x 归零、宽度不动，就会多遮 30px——
    第三层评测量的正是"非目标被误遮"，这种偏差不该由界面制造。
    """
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError) as exc:
        raise ValueError("坐标必须是整数") from exc
    if w <= 2 or h <= 2:
        raise ValueError(f"框太小（{w}×{h}），至少 3×3 像素")
    if x < 0:
        w += x
        x = 0
    if y < 0:
        h += y
        y = 0
    x = min(x, width - 1)
    y = min(y, height - 1)
    w = min(w, width - x)
    h = min(h, height - y)
    if w <= 2 or h <= 2:
        raise ValueError(f"框被钳到图内后不足 3×3（{w}×{h}），检查坐标是否落在图外")
    return [x, y, w, h]


def make_manual_box(
    x, y, w, h, *, width: int, height: int, seq: int, method: str = DEFAULT_METHOD
) -> dict:
    """手动补框。**不伪造 Detection**：它是 `finding_id=null` 的计划区域。"""
    if method not in METHODS:
        raise ValueError(f"处置方式必须是 {METHODS} 之一，实际 {method!r}")
    return {
        "id": f"manual-{seq:03d}",
        "bbox": clamp_bbox(x, y, w, h, width=width, height=height),
        "method": method,
    }


def boxes_revision(boxes: Sequence[Mapping]) -> str:
    """补框的内容版次，形如 `sha256:1a2b3c4d5e6f`。

    **为什么是内容指纹而不是计数器**：计数器存在会话内存里，程序重启就归零。
    B 看到上一批 `revision=5`、这一批 `revision=1`，会误判「更旧」。
    指纹没有这个毛病——框一样就是一样，改一个像素就变。

    **`id` 不参与**：用户删掉一个框再加回来，会拿到新的 `manual-002`，
    但坐标和手法完全相同，对 B 来说计划区域没变，不该算新批次。

    **`method` 参与**：同一块区域从 `solid` 改成 `mosaic`，坐标没变但遮盖效果变了，
    必须算新批次，否则 B 的复检报告会对不上。
    """
    canon = sorted(
        (
            tuple(int(v) for v in (box.get("bbox") or ())),
            str(box.get("method") or "solid"),
        )
        for box in boxes
    )
    blob = json.dumps(canon, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def export_payload(
    image_id: str | None,
    decisions: Sequence[Mapping],
    manual_boxes: Sequence[Mapping],
) -> dict:
    """导给 B 的交接包。

    `decisions` 是**合法的契约记录**（可直接过 validate_records）；
    `manual_boxes` 不是契约记录，是构造 RedactionPlan 的素材——B 把它们转成
    `finding_id=null` 的计划区域。两者分开摆，是为了不让 B 误以为补框也是 Detection。

    `manual_boxes_revision` 是**补框的内容版次**（见 `boxes_revision`）。Decision 的
    `revision` 只管勾选变化，补框变化它看不出来——B 要判断"这份包和上一份是不是同一批框"，
    只能看这个字段。它不进 schema：`manual_boxes` 本身就不是契约记录，没有 schema 可改。
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "image_id": image_id,
        "decisions": list(decisions),
        "manual_boxes": list(manual_boxes),
        "manual_boxes_revision": boxes_revision(manual_boxes),
        "note": "metadata 始终整图清除，不含 Decision；manual_boxes 仅供构造 RedactionPlan",
    }
