"""闸门定义 —— 回答「到节点了吗」。

闸门不是「全部检查通过」，而是「规格卡明确要求的那几项通过」。
W8 之前，UI 的处置页没做完不该拦住 W6；但 lock 没封存就该拦住 W6。
"""

from __future__ import annotations

from audit.model import Status

GATES: list[tuple[str, str, str]] = [
    ("W4", "dev 集 40 张 + 标注完成", "2026-11-01"),
    ("W6", "lock 集封存 + B 的 redact 可用", "2026-11-22"),
    ("W8", "可演示原型 + 第一版基线表（n≥30）", "2026-12-13"),
]

BLOCKING = (Status.FAIL, Status.PARTIAL)


def evaluate(results, gate_name: str) -> tuple[bool, list[str]]:
    """返回 (是否通过, 未达标项描述列表)。

    只有 FAIL / PARTIAL 拦闸门。SKIP 表示「无法判定」（比如依赖别人还没产出），
    算作未达标会让闸门永远红着，反而失去信号价值。
    """
    missing: list[str] = []
    for chk, res in results:
        if gate_name in chk.gate and res.status in BLOCKING:
            missing.append(f"{chk.id} {chk.title}（{res.evidence}）")
    return (not missing), missing
