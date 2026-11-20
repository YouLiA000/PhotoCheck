"""检查项汇总入口。

导入顺序决定报告里卡片的排列顺序，别随意打乱。
新增检查模块时在这里加一行 import。
"""

from audit.checks import contract, dataset, docs, env, evals, privacy, r04, ui  # noqa: F401

__all__ = ["contract", "env", "dataset", "evals", "ui", "privacy", "docs", "r04"]
