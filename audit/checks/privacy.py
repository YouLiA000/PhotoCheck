"""隐私红线检查（规格卡 §6 + 待办 4.5）。

这是**唯一一类「做对了不加分，做错了全盘皆输」的检查**：
体检工具如果把原图或完整敏感文本写进日志/CSV，本身就是新的泄露源。

注意：`dataset/` 与 `contract/examples/` 里的敏感号是**真值与样例**，是必需的，
不在扫描范围内。红线针对的是「运行产物」。
"""

from __future__ import annotations

import re

from audit.model import Context, Result, check, fail, ok, partial, skip

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

SCAN_DIRS = ("reports", "results", "logs", "out", "output")
SCAN_EXT = {".md", ".csv", ".json", ".txt", ".log"}


@check("PV-01", "隐私红线", "掩码逻辑单点实现（masking 模块）", owner="C", gate=("W6", "W8"))
def masking_module(ctx: Context) -> Result:
    """要求：全项目只有一个 masking 模块，且**真被 import**。

    原来的判定是"文件里出现 mask 字样就算引用"，太松——`masked_preview` 这个字段名
    本身就含 mask，光读字段的模块也会被算成使用者。改成查 import 语句。
    """
    cands = []
    for base in (ctx.src_dir, ctx.ui_dir, ctx.root / "contract"):
        if base.is_dir():
            cands += list(base.rglob("masking*.py"))
    if not cands:
        return fail("全项目找不到 masking 模块",
                    "K-08 / §0.5 红线 1 要求掩码只在单点实现，禁止各处自行拼接")

    # 只认 import 语句：from contract.masking import ... / import masking
    pat = re.compile(r"^\s*(from\s+[\w.]*masking\s+import|import\s+[\w.]*masking)")
    users = []
    for base in (ctx.src_dir, ctx.ui_dir, ctx.root / "dataset", ctx.root / "eval"):
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if p.name.startswith("masking"):
                continue
            if any(pat.match(line) for line in ctx.read_text(p).splitlines()):
                users.append(p.relative_to(ctx.root).as_posix())
    if not users:
        return partial(f"有 {cands[0].name} 但没被任何模块 import",
                       "实现完要真的接进检测与处置流程")
    if len(cands) > 1:
        return fail(f"有 {len(cands)} 份掩码实现："
                    + "、".join(p.name for p in cands), "只留一份，其余改为 import")
    return ok(f"{cands[0].relative_to(ctx.root).as_posix()} ← 被 {len(users)} 个模块引用")


@check("PV-02", "隐私红线", "运行产物里没有完整手机号/身份证号", gate=("W6", "W8"))
def no_leak_in_outputs(ctx: Context) -> Result:
    hits, scanned = [], 0
    for name in SCAN_DIRS:
        base = ctx.root / name
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in SCAN_EXT:
                continue
            scanned += 1
            text = ctx.read_text(p)
            if PHONE.search(text) or IDCARD.search(text):
                hits.append(p.relative_to(ctx.root).as_posix())
    if hits:
        return fail(f"{len(hits)} 个文件含完整敏感号：{', '.join(hits[:3])}",
                    "掩码后再落盘；这条一旦在评审时被发现，整个方案的可信度就没了")
    if scanned == 0:
        return skip("暂无运行产物目录（reports/results/logs 都不存在）",
                    "等 eval.py 产出结果后这条才有意义")
    return ok(f"扫描 {scanned} 个文件，无完整敏感号")


@check("PV-03", "隐私红线", "界面测试覆盖「不漏完整号码」", gate=("W8",))
def ui_leak_test(ctx: Context) -> Result:
    t = ctx.ui_dir / "tests"
    if not t.is_dir():
        return skip("没有界面测试")
    hits = [p.name for p in t.glob("*.py") if "漏" in ctx.read_text(p) or "leak" in ctx.read_text(p).lower()]
    if hits:
        return ok(f"{hits[0]} 有相关断言")
    return partial("界面测试里没看到防泄露断言", "清单与结论面板必须只显示掩码信息")


@check("PV-04", "隐私红线", "禁止依赖：PaddleOCR / PySide6 / 联网 API", gate=("W4",))
def forbidden_deps(ctx: Context) -> Result:
    banned = ("paddleocr", "pyside6", "requests.post", "openai", "anthropic")
    found = []
    for req in ("requirements.txt", "requirements-dev.txt"):
        p = ctx.root / req
        if p.is_file():
            low = ctx.read_text(p).lower()
            found += [b for b in banned[:2] if b in low]
    for base in (ctx.src_dir, ctx.ui_dir, ctx.contract_dir):
        if base.is_dir():
            for p in base.rglob("*.py"):
                low = ctx.read_text(p).lower()
                found += [b for b in banned[2:] if b in low]
    if found:
        return fail(f"出现禁用项 {sorted(set(found))}", "规格卡 §6：换库需三人同意，联网 API 一律禁止")
    return ok("依赖清单与代码里均无禁用项")
