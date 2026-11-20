"""评测口径常量（K-13）。

改这里的任何一项都等于改评测标准，需要三人同意并升版本号。
"""

from __future__ import annotations

# 对象级匹配：规格卡 K-13「工具」表写死 IoU >= 0.5 且 subtype 相同
IOU_THRESHOLD = 0.5
REQUIRE_SAME_SUBTYPE = True

# 报告纪律：n < 30 时标注「仅作开发参考」，不得据此下通用结论
MIN_N_FOR_CONCLUSION = 30
SMALL_SAMPLE_NOTE = "仅作开发参考"

# 敏感性分析用（不进主表）：文本类放宽到 0.3
LOOSE_IOU_TEXT = 0.3
TEXT_KINDS = ("text",)

# 元数据没有 bbox，按 subtype 在整图范围内配对
META_KIND = "meta"
