"""PhotoCheck 进度审计器。

    python audit/run.py            # 终端报告
    python audit/run.py --md --json

新加检查见 audit/checks/ 下任一模块；改闸门见 audit/gates.py。
"""

__all__ = ["model", "gates", "checks", "run"]
