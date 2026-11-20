#!/usr/bin/env python3
"""PhotoCheck 进度审计器 —— 用「项目事实」回答两个问题：

    1. 进度：现在到哪了，离下一个闸门还差什么？
    2. 合规：规格卡的验收要求，哪些真达标、哪些只是"看起来做了"？

跑法：

    python audit/run.py                 # 终端报告
    python audit/run.py --md            # 顺便写 docs/AUDIT.md
    python audit/run.py --json          # 顺便写 audit/latest.json（便于对比历史）
    python audit/run.py --gate W6       # 只看某个闸门
    python audit/run.py --card K-13     # 只看某张卡
    python audit/run.py -v              # 连通过项的证据一起打印

加一条检查：在 `audit/checks/` 里写个函数挂 `@check(...)` 装饰器即可，
`audit/checks/__init__.py` 会自动导入，不需要改本文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit import checks as _checks  # noqa: F401  触发注册
from audit.gates import GATES, evaluate
from audit.model import STATUS_ICON, Context, Status, run_all

AUDITED_PATHS = (
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "app",
    "contract",
    "dataset",
    "eval",
    "photocheck",
    "requirements.txt",
    "requirements-dev.txt",
    "run.bat",
    "scripts",
    "tools",
    "ui",
    "README.md",
)


def audited_head(ctx: Context) -> str:
    """Return the latest commit that changed audited source, not audit metadata.

    A generated audit file cannot contain the hash of the commit that will
    include that same file. Tracking the last source-changing commit avoids a
    permanently stale-looking ``head`` after the docs-only snapshot commit.
    """

    return ctx.git("log", "-1", "--format=%h", "--", *AUDITED_PATHS) or "?"


def group_by_card(results):
    groups: dict[str, list] = {}
    for chk, res in results:
        groups.setdefault(chk.card, []).append((chk, res))
    return groups


def tally(results):
    c = {s: 0 for s in Status}
    for _, r in results:
        c[r.status] += 1
    return c


def card_line(name: str, items) -> str:
    c = tally(items)
    return f"{name}   {c[Status.PASS]}/{len(items)}"


def render_terminal(results, verbose: bool) -> str:
    ctx = Context(ROOT)
    head = audited_head(ctx)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"PhotoCheck 进度审计  {now}  HEAD {head}  Python "
           f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "=" * 78]

    for card, items in group_by_card(results).items():
        out.append("")
        out.append(card_line(card, items))
        for chk, res in items:
            if not verbose and res.status is Status.PASS:
                continue
            out.append(f"  {STATUS_ICON[res.status]}  {chk.id}  {chk.title} — {res.evidence}")
            if res.hint and res.status is not Status.PASS:
                out.append(f"        → {res.hint}")

    out.append("")
    out.append("-" * 78)
    out.append("闸门")
    for name, desc, due in GATES:
        passed, missing = evaluate(results, name)
        if passed:
            out.append(f"  {name}  通过      {desc}")
        else:
            out.append(f"  {name}  未通过    差 {len(missing)} 项 — {desc}")
            for m in missing:
                out.append(f"       · {m}")

    c = tally(results)
    out.append("")
    out.append("-" * 78)
    out.append(f"合计 PASS {c[Status.PASS]} / PARTIAL {c[Status.PARTIAL]} / "
               f"FAIL {c[Status.FAIL]} / SKIP {c[Status.SKIP]}   共 {len(results)} 项")

    by_owner: dict[str, list] = {}
    for chk, res in results:
        by_owner.setdefault(chk.owner, []).append((chk, res))
    seg = []
    for owner, items in sorted(by_owner.items()):
        cc = tally(items)
        seg.append(f"{owner} {cc[Status.PASS]}/{len(items)}")
    out.append("按归属  " + "   ".join(seg))

    fails = [(chk, r) for chk, r in results if r.status is Status.FAIL]
    p0_fails = [c for c, _ in fails if c.level == "P0"]
    if p0_fails:
        out.append("")
        out.append(f"[!] P0 未达标 {len(p0_fails)} 项：" + "、".join(c.id for c in p0_fails))
    return "\n".join(out)


def _esc(text) -> str:
    """表格里不能出现裸的竖线，会把列截断。"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(results) -> str:
    ctx = Context(ROOT)
    head = audited_head(ctx)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 进度审计报告",
        "",
        f"> 自动生成，勿手改。重跑：`python audit/run.py --md`",
        f"> {now} ｜ HEAD `{head}` ｜ Python "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "",
    ]

    lines += ["## 闸门", "", "| 闸门 | 状态 | 要求 | 未达标项 |", "|---|---|---|---|"]
    for name, desc, due in GATES:
        passed, missing = evaluate(results, name)
        status = "通过" if passed else f"未通过（差 {len(missing)}）"
        lines.append(f"| {name}（{due}） | {status} | {desc} | "
                     f"{'<br>'.join(_esc(m) for m in missing) or '—'} |")

    lines += ["", "## 明细", ""]
    for card, items in group_by_card(results).items():
        c = tally(items)
        lines += [f"### {card}（{c[Status.PASS]}/{len(items)}）", "",
                  "| 结果 | 编号 | 检查项 | 证据 | 下一步 |", "|---|---|---|---|---|"]
        for chk, res in items:
            lines.append(f"| {STATUS_ICON[res.status]} | {chk.id} | {_esc(chk.title)} | "
                         f"{_esc(res.evidence)} | {_esc(res.hint or '—')} |")
        lines.append("")

    c = tally(results)
    lines += ["## 统计", "",
              f"- PASS {c[Status.PASS]} / PARTIAL {c[Status.PARTIAL]} / "
              f"FAIL {c[Status.FAIL]} / SKIP {c[Status.SKIP]}，共 {len(results)} 项", ""]
    return "\n".join(lines)


def render_json(results) -> dict:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "head": audited_head(Context(ROOT)),
        "checks": [
            {"id": c.id, "card": c.card, "title": c.title, "level": c.level,
             "owner": c.owner, "status": r.status.value, "evidence": r.evidence,
             "hint": r.hint}
            for c, r in results
        ],
        "gates": {name: {"passed": evaluate(results, name)[0],
                         "missing": evaluate(results, name)[1]}
                  for name, _, _ in GATES},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PhotoCheck 进度审计")
    ap.add_argument("--md", action="store_true", help="写 docs/AUDIT.md")
    ap.add_argument("--json", action="store_true", help="写 JSON 快照（默认 audit/latest.json）")
    ap.add_argument("--json-out", default=None,
                    help="JSON 快照路径；给自动化用，避免覆盖仓库里的历史快照")
    ap.add_argument("--json-stdout", action="store_true",
                    help="把 JSON 打到标准输出（供脚本解析，不写文件）")
    ap.add_argument("--gate", default=None, help="只看某个闸门，如 W6")
    ap.add_argument("--card", default=None, help="只看某张卡，如 K-13")
    ap.add_argument("-v", "--verbose", action="store_true", help="连通过项也打印")
    args = ap.parse_args(argv)

    ctx = Context(ROOT)
    results = run_all(ctx)

    if args.card:
        results = [(c, r) for c, r in results if args.card.lower() in c.card.lower()]
    if args.gate:
        results = [(c, r) for c, r in results if args.gate in c.gate]

    if not results:
        print("没有匹配的检查项。")
        return 2

    # --json-stdout 是给脚本用的：终端报告会污染 stdout 上的 JSON
    if not args.json_stdout:
        print(render_terminal(results, args.verbose))

    payload = render_json(results)
    if args.json_stdout:
        print(json.dumps(payload, ensure_ascii=False))
    if args.md:
        p = ctx.docs_dir / "AUDIT.md"
        p.write_text(render_markdown(results) + "\n", encoding="utf-8", newline="\n")
        print(f"\n已写入 {p.relative_to(ROOT).as_posix()}")
    if args.json or args.json_out:
        p = Path(args.json_out) if args.json_out else ROOT / "audit" / "latest.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8", newline="\n")
        print(f"已写入 {p}")

    fails = [r for _, r in results if r.status is Status.FAIL]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
