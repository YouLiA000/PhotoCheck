#!/usr/bin/env python3
"""一条命令跑完全部验收（K-15「每周五冒烟」的落地）。

为什么要有这个：验收要跑四套测试 + 环境自检 + 审计 + 评测自测，六条命令、三个不同的
工作目录。靠人记，周五就会漏一条；漏的那条恰好是红的，就要拖到下周才发现。

    python tools/smoke.py            # 全跑，一分钟内（审计里的子进程检查最慢）
    python tools/smoke.py --quick    # 跳过评测自测（最慢的一步）

退出码：0 = 全绿；1 = 有红的；2 = 脚本自身出错。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_configured_python = os.environ.get("PHOTOCHECK_PYTHON")
PY = Path(_configured_python) if _configured_python else Path(sys.executable)


def _env() -> dict:
    """清掉 PYTHONPATH：工具注入的站点定制会拦 pip / 干扰子进程（见 AGENTS §6）。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [str(a) for a in args], cwd=str(cwd or ROOT), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=_env(),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _audit_c_only() -> tuple[bool, str]:
    """审计里 A / B 的缺口不该让 C 的冒烟变红——那是催人，不是自己坏了。

    读 `--json-stdout` 的结构化结果，不再用正则去猜那一行「C 38/38」：
    正则版本一旦审计的输出格式微调（加个括号、换个空格）就会静默匹配不上，
    然后 `match` 失败走到"看退出码"分支，冒烟又变回"跟着 A/B 一起红"。
    """
    code, out = _run([PY, "audit/run.py", "--json-stdout"])
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip().startswith("{")), "")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False, _tail(out)
    if code not in (0, 1):
        return False, f"审计进程异常（退出码 {code}）：{_tail(out)}"

    checks = payload.get("checks") or []
    mine = [c for c in checks if c.get("owner") == "C"]
    passed = [c for c in mine if c.get("status") == "PASS"]
    skipped = [c for c in mine if c.get("status") == "SKIP"]
    if not mine:
        return False, "审计里没有 C 名下的检查项（归属字段被改过？）"
    if len(passed) + len(skipped) != len(mine):
        bad = [
            f"{c['id']} {c['title']}"
            for c in mine
            if c.get("status") not in {"PASS", "SKIP"}
        ]
        return False, f"C 侧 {len(passed)}/{len(mine)}；未通过：{' / '.join(bad)[:150]}"
    suffix = f"，{len(skipped)} 项明确跳过" if skipped else ""
    return True, f"C 侧 {len(passed)}/{len(mine)} PASS{suffix}（A/B 缺口不计入）"


def _tail(text: str, lines: int = 3) -> str:
    rows = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " / ".join(rows[-lines:])[:160]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PhotoCheck 冒烟测试")
    ap.add_argument("--quick", action="store_true",
                    help="跳过进度审计（约 40s，最慢的一项）与评测自测；日常改代码用，交付前跑全量")
    args = ap.parse_args(argv)

    if not PY.exists():
        print(f"找不到项目解释器：{PY}\n请设置 PHOTOCHECK_PYTHON 指向 Python 3.11")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="photocheck-smoke-"))
    steps: list[tuple[str, list[str]]] = [
        ("环境自检", [PY, "tools/check_env.py"]),
        ("契约层", [PY, "-m", "unittest", "discover", "-s", "contract/tests"]),
        ("数据工具", [PY, "-m", "unittest", "discover", "-s", "dataset/tools/tests"]),
        ("界面", [PY, "-m", "unittest", "discover", "-s", "ui/tests"]),
        ("评测逻辑", [PY, "-m", "unittest", "discover", "-s", "eval/tests"]),
        ("运维工具", [PY, "-m", "unittest", "discover", "-s", "tools/tests"]),
        ("lock 封存", [PY, "dataset/tools/seal_lock.py", "verify"]),
    ]
    if not args.quick:
        steps.append(("评测自测", [PY, "eval/eval.py", "--set", "dev", "--perfect",
                                   "--ci", "200", "--out", str(tmp)]))

    print(f"PhotoCheck 冒烟  {time.strftime('%Y-%m-%d %H:%M')}  根目录 {ROOT.name}/")
    print("-" * 66)

    failures = []
    started = time.time()
    for name, cmd in steps:
        t0 = time.time()
        code, out = _run(cmd)
        dt = time.time() - t0
        ok = code == 0
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark} {name:<10} {dt:5.1f}s   {_tail(out) if not ok else ''}".rstrip())
        if not ok:
            failures.append((name, out))

    if args.quick:
        print(f"  --   {'进度审计':<10}   跳过（--quick）")
    else:
        t0 = time.time()
        ok, detail = _audit_c_only()
        print(f"  {'OK  ' if ok else 'FAIL'} {'进度审计':<10} {time.time() - t0:5.1f}s   {detail}")
        if not ok:
            failures.append(("进度审计（C 归属）", detail))

    elapsed = time.time() - started
    print("-" * 66)
    if failures:
        print(f"共 {len(failures)} 项未通过（{elapsed:.1f}s）：")
        for name, out in failures:
            print(f"\n【{name}】")
            print("\n".join(out.strip().splitlines()[:12]))
        shutil.rmtree(tmp, ignore_errors=True)   # 失败时也清，但先打印完再清
        return 1
    print(f"全部通过：{len(steps) + 1} 项，{elapsed:.1f}s")
    shutil.rmtree(tmp, ignore_errors=True)   # 每次冒烟留一个临时目录，攒半年就是垃圾
    return 0


if __name__ == "__main__":
    sys.exit(main())
