#!/usr/bin/env python3
"""K-10 风险解释：把 `Assessment` 翻成一句人话。

**这一层只做措辞，不重新算分。**算分全在 `contract/risk_rules.py`：
  `risk_rules.assess()`    分值、分档、组合加成、与检测器自带值的一致性
  `risk_explain.explain()` 中文标签、句式、K-12 面板
分开的理由是两者都会出错，但错法完全不同：分算错是口径问题（要三人同意才能改），
措辞错是越界问题（一句话就把"未检出"说成"安全"）。混在一起就没法分别审。

**为什么不让界面自己拼字符串**：规格卡 K-10 验收写着「文案全走模板，代码无自由拼接」。
散在各处拼，同一张图会出现三种说法，而 §0.5 又禁止「已确保安全」「可以放心分享」这类
表述——分散写法根本没法统一审查。所以句式、标签、证据名全部收在这里。

规格卡依据：K-10 两个维度（severity + evidence）、两条组合加成、展示格式示例、
score 纪律（不得写成概率）、§0.5 措辞红线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from contract.risk_rules import SEVERITY_HIGH, SEVERITY_LOW, assess, evidence_code

# --------------------------------------------------------------------------
# 词表：三处展示（清单 / 面板 / 报告）必须用同一套，不许各自维护
# --------------------------------------------------------------------------

KIND_LABEL = {"meta": "元数据", "text": "文字", "qr": "二维码", "face": "人脸"}

SUBTYPE_LABEL = {
    "gps": "GPS 坐标", "datetime": "拍摄时间", "device": "设备型号",
    "serial": "设备序列号", "thumbnail": "内嵌缩略图", "software": "处理软件",
    "phone": "手机号", "idcard": "身份证号", "face_region": "人脸区域",
    "qr_vcard": "名片 / 联系人", "qr_wifi": "Wi-Fi 密码", "qr_wechat_pay": "收款码",
    "qr_alipay": "收款码", "qr_link": "普通链接", "qr_code": "条码",
    "qr_unknown": "未识别二维码", "qr_wechat_card": "微信名片",
    "qr_wechat_group": "群二维码", "qr_contact": "联系人", "qr_geo": "位置码",
}

# K-10 要求 severity 与 evidence 分两列；evidence 由 kind 固定推出，不由检测器自由填
EVIDENCE_LABEL = {
    "exif": "EXIF 元数据",
    "ocr_rule": "文字识别结果",
    "qr_payload": "二维码内容",
    "face_box": "人脸检测",
}

# 每条发现的入句短语（模板槽位）。**不带"含"字**——句子模板统一加，避免"含…、含…"重复
PHRASE = {
    "gps": "GPS 坐标",
    "datetime": "拍摄时间",
    "device": "设备型号",
    "serial": "设备序列号",
    "thumbnail": "内嵌缩略图",
    "software": "处理软件信息",
    "phone": "手机号",
    "idcard": "身份证号",
    "face_region": "人脸区域",
    "qr_wifi": "Wi-Fi 密码二维码",
    "qr_wechat_pay": "收款码",
    "qr_alipay": "收款码",
    "qr_vcard": "名片 / 联系人码",
    "qr_contact": "联系人码",
    "qr_wechat_card": "微信名片码",
    "qr_wechat_group": "群二维码",
    "qr_geo": "位置码",
    "qr_link": "普通链接码",
    "qr_code": "条码",
    "qr_unknown": "未识别二维码",
}

# 加成文案。键与 risk_rules.BONUS_RULES 的 subtype 对一致（有测试守着两边不脱节）
BONUS_LABEL = {
    ("gps", "face_region"): "组合加成 GPS + 人脸（+2）",
    ("gps", "qr_wifi"): "组合加成 GPS + Wi-Fi 密码（+3）",
}

# 规格卡 K-10 展示格式最后那半句，原样使用。这是唯一允许出现的"建议"
ADVICE = "建议逐项确认后再决定是否分享。"

_EMPTY_SENTENCE = (
    "**低风险**（来源：未检出）：本次未检出手机号、身份证号、二维码、敏感元数据"
    "这四类线索。结果只覆盖本工具能自动识别的对象。"
)


def label_of(detection: Mapping) -> str:
    """展示用的中文子类名；未知 subtype 原样返回，不吞掉信息。"""
    sub = str(detection.get("subtype"))
    return SUBTYPE_LABEL.get(sub, sub)


def phrase_of(detection: Mapping) -> str:
    sub = str(detection.get("subtype"))
    return PHRASE.get(sub, SUBTYPE_LABEL.get(sub, sub))


def evidence_of(detection: Mapping) -> str:
    """evidence 的中文名。未知代码返回原值，方便发现枚举漏了哪一类。"""
    code = evidence_code(detection)
    return EVIDENCE_LABEL.get(code, code or "—")


@dataclass(frozen=True)
class RiskExplanation:
    """K-10 的 (severity, evidence, 说明, 建议) 四元组，外加可核对的明细。

    所有字段都由 `risk_rules.assess()` 的结果派生，本层不引入新的判断。
    """

    level: str
    score: int
    base_score: int
    bonuses: list[tuple[str, int]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    items: list[tuple[str, int]] = field(default_factory=list)
    counts: list[tuple[str, int]] = field(default_factory=list)
    mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    # 分值来自 PROVISIONAL_SCORE（规格卡没写、按同类推的），展示时必须能看见
    unconfirmed: list[str] = field(default_factory=list)
    sentence: str = ""

    @property
    def is_high(self) -> bool:
        return self.level == SEVERITY_HIGH


def explain(detections: Iterable[Mapping]) -> RiskExplanation:
    """整图风险解释。**唯一的措辞入口**，界面与报告都调它。"""
    dets = list(detections)
    result = assess(dets)

    if not dets:
        return RiskExplanation(
            level=SEVERITY_LOW, score=0, base_score=0, bonuses=[], evidence=[],
            items=[], counts=[], mismatches=[], unconfirmed=[], sentence=_EMPTY_SENTENCE,
        )

    # 来源：按出现顺序去重，列全而不是简写成"多来源"
    codes: list[str] = []
    for det in dets:
        code = evidence_code(det)
        if code not in codes:
            codes.append(code)
    evidence_labels = [EVIDENCE_LABEL.get(code, code or "—") for code in codes]

    bonus_texts = [
        (BONUS_LABEL.get(pair, f"组合加成 {pair[0]} + {pair[1]}（+{extra}）"), extra)
        for pair, extra in result.bonuses
    ]

    body = "、".join(f"{PHRASE.get(sub, SUBTYPE_LABEL.get(sub, sub))}（{risk}）"
                     for sub, risk in result.items)
    # 中文和拉丁字母之间留一格空格，但"含手机号"不该断开
    lead = "含 " if body[:1].isascii() else "含"
    sentence = f"**{result.level}风险**（来源：{' + '.join(evidence_labels)}）：{lead}{body}"
    if bonus_texts:
        sentence += "；另有" + "、".join(text for text, _ in bonus_texts)
    sentence += f"，合计 {result.score} 分。{ADVICE}"

    # 计数按 subtype 合并同展示名的（qr_wechat_pay / qr_alipay 都叫"收款码"）
    counts: dict[str, int] = {}
    for det in dets:
        name = label_of(det)
        counts[name] = counts.get(name, 0) + 1
    count_list = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return RiskExplanation(
        level=result.level, score=result.score, base_score=result.base_score,
        bonuses=bonus_texts, evidence=evidence_labels,
        items=[(PHRASE.get(sub, SUBTYPE_LABEL.get(sub, sub)), risk)
               for sub, risk in result.items],
        counts=count_list, mismatches=result.mismatches,
        unconfirmed=result.unconfirmed, sentence=sentence,
    )


def panel_markdown(detections: Iterable[Mapping], plan_revision: int | None = None) -> str:
    """K-12 结论面板。**只允许三块内容**：等级 + 一句话、计数、复检两条结论。

    这里不放原图、不放原文、不给导出入口。
    """
    exp = explain(detections)
    lines = [
        f"### {exp.level}风险 · 合计 {exp.score} 分",
        "",
        exp.sentence,
        "",
    ]

    if exp.counts:
        lines += ["**风险项计数**（按子类，不含原文）", "", "| 子类 | 数量 |", "|---|---|"]
        lines += [f"| {name} | {n} |" for name, n in exp.counts]
        lines.append("")
    else:
        lines += ["**风险项计数**：0 项。", ""]

    if exp.unconfirmed:
        # 分值不是规格卡写死的，是推的。藏起来就是拿推定值冒充实测结论
        names = "、".join(SUBTYPE_LABEL.get(s, s) for s in exp.unconfirmed)
        lines += [f"> {names} 的分值尚未经三人确认（`PROVISIONAL_SCORE`），合计分含推定成分。", ""]

    lines += ["**复检结论**", ""]
    if plan_revision is None:
        lines += [
            "- 处置有效性：**尚未执行**（B 的 K-09 在 W7 接入）",
            "- 已知保留项与复检异常：**尚未执行**",
        ]
    else:
        lines += [
            f"- 处置有效性：绑定 plan_revision = {plan_revision}",
            "- 已知保留项与复检异常：见复检器输出",
        ]

    if exp.mismatches:
        # 这条不进报告正文，但界面必须让人看见：A 填的 risk 和规则算的对不上
        lines += ["", "> 检测器自带 risk 与规则不一致，已按规则显示：" +
                  "、".join(f"{i}（{d}→{c}）" for i, d, c in exp.mismatches)]

    return "\n".join(lines)
