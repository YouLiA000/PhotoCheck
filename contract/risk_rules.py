#!/usr/bin/env python3
"""风险分值规则（K-04 / K-10）。三人共用一份，A 产出 Detection.risk、C 评测都调它。

为什么要单独一个模块：
  `Detection.risk` 是契约必填字段，A 的检测器要填，C 的评测要读。两边各写一份规则，
  同一张图两个 risk 值，联调时必然扯皮。规则只有一处实现。

**分层**（改之前先看清自己在改哪层）：
  - 本模块：算分。分值表、单对象分值、整图分档、组合加成、与检测器自带值的一致性
  - `risk_explain.py`：措辞。只把这里的结论翻成中文句子，不重新算
  分开是为了让"分算得对不对"和"话说得对不对"能分别审。

规格卡依据：
  - K-04：手机号 = 2；身份证 pass = 3 / fail = 2
  - K-10（S-3 修订）：身份证(pass)/Wi-Fi 密码/精确 GPS/支付收款码 = 3；手机号/校验位未通过 = 2；
          人脸/普通链接/设备型号 = 1
  - K-12 展示示例：设备序列号 = 2
  - K-10 分档：≥5 高 / 3–4 中 / ≤2 低；组合加成 GPS+人脸 = +2，Wi-Fi 二维码+GPS = +3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Tuple

CONTRACT_VERSION = "photocheck/v4"

# 规格卡明确写死的分值（改了要三人同意）
SPEC_SCORE = {
    ("meta", "gps"): 3,
    ("meta", "device"): 1,
    ("meta", "serial"): 2,
    ("text", "phone"): 2,
    ("qr", "qr_wifi"): 3,
    ("qr", "qr_wechat_pay"): 3,   # 收款码：S-3 最终口径
    ("qr", "qr_alipay"): 3,       # 收款码：S-3 最终口径
    ("qr", "qr_link"): 1,         # 普通链接
    ("face", "face_region"): 1,
}

# 规格卡 K-10 没写、或 K-06 写得更具体的分值 —— **待三人确认**。
#
# 取值依据（2026-11-07 逐行核对规格卡后修正，不再用"同类推理"）：
#   K-06 §830  名片 / 联系人 → 3
#   K-06 §831  收款 / 支付   → 3   ← S-3 已确认覆盖 K-10 旧汇总表
#   K-06 §832  Wi-Fi        → 3   （与 K-10 一致，已在 SPEC_SCORE）
#   K-06 §833  普通链接      → 1   （与 K-10 一致，已在 SPEC_SCORE）
#   K-06 §834  未识别        → 1
#
# 注意 qr_unknown = 1 是**规格卡原文**，不是"读不出来就低风险"的推断。
# A 主张升到 2（"知道得最少不能给最低风险"），理由成立但那是改规格卡，
# 应走 docs/规格卡修订建议/，不能由实现单方面偏离。
PROVISIONAL_SCORE = {
    ("meta", "datetime"): 2,        # 规格卡未写。可精确到秒并与位置交叉，介于设备型号 1 与精确 GPS 3 之间
    ("meta", "thumbnail"): 3,       # 规格卡未写。缩略图是原图副本，可能带完整 GPS —— 等同"精确 GPS"
    ("meta", "software"): 1,        # 规格卡未写。编辑软件名，等同"设备型号"
    ("qr", "qr_vcard"): 3,          # K-06 §830 名片/联系人
    ("qr", "qr_contact"): 3,        # K-06 §830 名片/联系人
    ("qr", "qr_wechat_card"): 3,    # K-06 §830 微信名片
    ("qr", "qr_wechat_group"): 3,   # S-3：可能入群并查看历史，按最坏情况取保守上界
    ("qr", "qr_geo"): 3,            # S-3：可能定位到具体地点，按最坏情况取保守上界
    ("qr", "qr_code"): 1,           # 规格卡未写。裸条码 ≈ 普通链接
    ("qr", "qr_unknown"): 1,        # K-06 §834 未识别
}

UNCONFIRMED = sorted(PROVISIONAL_SCORE)

SEVERITY_HIGH = "高"
SEVERITY_MID = "中"
SEVERITY_LOW = "低"

# kind → evidence 的固定对应（契约 §0.2）。Detection 自带 evidence 时以它为准
KIND_TO_EVIDENCE = {
    "meta": "exif",
    "text": "ocr_rule",
    "qr": "qr_payload",
    "face": "face_box",
}

# 组合加成：(两个 subtype, 加几分)。**只保留规格卡写死的这两条**
BONUS_RULES: tuple[tuple[str, str, int], ...] = (
    ("gps", "face_region", 2),
    ("gps", "qr_wifi", 3),
)

# 分档阈值（K-10）
HIGH_FROM = 5
MID_FROM = 3


def detection_risk(kind: str, subtype: str, details: Mapping | None = None) -> int:
    """单个 Detection 的 risk。身份证按三态特殊处理（K-04）。"""
    details = details or {}
    if kind == "text" and subtype == "idcard":
        # K-04 v1.3 修正：校验位不通过仍然要报，只是降为 2，不是不报
        return 3 if details.get("idcard_check") == "pass" else 2
    key = (kind, subtype)
    if key in SPEC_SCORE:
        return SPEC_SCORE[key]
    if key in PROVISIONAL_SCORE:
        return PROVISIONAL_SCORE[key]
    raise KeyError(f"未定义分值的 subtype：{key}；请在 SPEC_SCORE 或 PROVISIONAL_SCORE 中补上")


def evidence_code(detection: Mapping) -> str:
    """契约里的 evidence 代码。Detection 自己带值就用它，否则按 kind 推。"""
    code = detection.get("evidence")
    if code:
        return str(code)
    return KIND_TO_EVIDENCE.get(str(detection.get("kind")), "")


def resolve_risk(detection: Mapping) -> tuple[int, str]:
    """(分值, 来源状态)。状态：rule / rule_mismatch / declared。

    规则里没定义的 subtype 不抛异常——界面不能因为 A 多回了一类就整页崩掉，
    退回检测器自带的值并标记 declared，让"这个分没有规则支撑"可见。
    """
    declared = detection.get("risk")
    try:
        computed = detection_risk(
            str(detection.get("kind")), str(detection.get("subtype")),
            detection.get("details") or {},
        )
    except KeyError:
        return int(declared or 1), "declared"
    if declared is not None and int(declared) != computed:
        return computed, "rule_mismatch"
    return computed, "rule"


@dataclass(frozen=True)
class Assessment:
    """整图风险评估。**唯一算分出口**，`risk_explain` 只负责把它说成人话。"""

    level: str
    score: int
    base_score: int
    # ((subtype_a, subtype_b), 加成值)
    bonuses: list[tuple[tuple[str, str], int]] = field(default_factory=list)
    # 按输入顺序的 (subtype, 分值)
    items: list[tuple[str, int]] = field(default_factory=list)
    # 去重后的 kind 顺序，供展示层翻成"来源"
    kinds: list[str] = field(default_factory=list)
    # (标识, 检测器填的值, 规则算的值)
    mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    # 用了推定分值的 subtype
    unconfirmed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items


def _level_of(score: int) -> str:
    if score >= HIGH_FROM:
        return SEVERITY_HIGH
    if score >= MID_FROM:
        return SEVERITY_MID
    return SEVERITY_LOW


def assess(detections: Iterable[Mapping]) -> Assessment:
    """整图风险。`Detection.risk` 是单对象 1/2/3；这里把它们**加总**成整图 severity。

    两个层级别混——一张图三个手机号是 2+2+2=6 判高风险，不是每个手机号都高风险。
    """
    dets = list(detections)

    # 组合加成按 subtype 判定，重复对象（两个手机号）不该重复加成
    subtypes = {str(d.get("subtype")) for d in dets}
    bonuses: list[tuple[tuple[str, str], int]] = [
        ((a, b), extra) for a, b, extra in BONUS_RULES
        if a in subtypes and b in subtypes
    ]

    items: list[tuple[str, int]] = []
    kinds: list[str] = []
    mismatches: list[tuple[str, int, int]] = []
    unconfirmed: list[str] = []
    base = 0

    for det in dets:
        risk, state = resolve_risk(det)
        sub = str(det.get("subtype"))
        base += risk
        items.append((sub, risk))
        if state == "rule_mismatch":
            mismatches.append((str(det.get("id") or sub), int(det.get("risk")), risk))
        kind = str(det.get("kind"))
        if kind not in kinds:
            kinds.append(kind)
        if (kind, sub) in PROVISIONAL_SCORE and sub not in unconfirmed:
            unconfirmed.append(sub)

    score = base + sum(extra for _, extra in bonuses)
    return Assessment(
        level=_level_of(score), score=score, base_score=base, bonuses=bonuses,
        items=items, kinds=kinds, mismatches=mismatches, unconfirmed=unconfirmed,
    )


def image_severity(detections: Iterable[Mapping]) -> Tuple[str, int, list[str]]:
    """兼容旧调用：返回 (等级, 合计分, 命中说明)。

    实现已改为调 `assess()`——**不再读检测器自带的 risk 求和**。两套算法并存时，
    只要 A 填错一个值，界面和报告就会对同一张图给出不同等级，而这种不一致
    在联调里极难查（两边各自都"算对了"）。说明字段仅供调试，正式文案走 risk_explain。
    """
    result = assess(detections)
    notes = [f"{sub}（{risk}）" for sub, risk in result.items]
    for (a, b), extra in result.bonuses:
        notes.append(f"组合加成 {a}+{b}（+{extra}）")
    return result.level, result.score, notes
