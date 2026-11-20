"""文档一致性检查 —— 防「文档漂移」。

这一类检查的价值：文档说 27 张、实际 40 张，这种事不发生一次就不知道有多贵。
特别是 DC-03：它自动验证 `docs/INDEX.md` 里的规格卡行号表**是否还对得上**。
规格卡一改版，行号全错，索引就从「省上下文」变成「误导」，而人很难察觉。
"""

from __future__ import annotations

import re
import time

from audit.model import Context, Result, check, fail, ok, partial, skip

SPEC_GLOB = "功能规格卡_v*.md"
ROW = re.compile(r"\|\s*\*{0,2}((?:K|R|D)-[\d/]+)\*{0,2}\s*\|[^|]*\|\s*(\d+)\s*\|")


def _spec_file(ctx: Context):
    cands = sorted(ctx.docs_dir.glob(SPEC_GLOB))
    return cands[-1] if cands else None


@check("DC-01", "文档", "README 写的 dev 张数与实际一致", gate=("W4",))
def readme_count(ctx: Context) -> Result:
    p = ctx.root / "README.md"
    if not p.is_file():
        return fail("缺 README.md")
    text = ctx.read_text(p)
    m = re.search(r"(\d+)\s*张合成\s*dev", text)
    if not m:
        return skip("README 里没写 dev 张数", "可以不写，但写了就必须对")
    claimed = int(m.group(1))
    actual = len(list((ctx.dev_dir / "images").glob("*.jpg")))
    if claimed != actual:
        return fail(f"README 写 {claimed}，实际 {actual}", "改了数据就同步改 README，否则读的人被误导")
    return ok(f"一致（{actual} 张）")


@check("DC-02", "文档", "HANDOFF 存在且在 7 天内更新", gate=("W4", "W6", "W8"))
def handoff_fresh(ctx: Context) -> Result:
    p = ctx.docs_dir / "HANDOFF.md"
    if not p.is_file():
        return fail("缺 docs/HANDOFF.md", "这是状态的唯一真相源，缺了就得靠翻五个文件")
    age_h = (time.time() - p.stat().st_mtime) / 3600
    if age_h > 24 * 7:
        return fail(f"已 {age_h / 24:.0f} 天未更新", "覆盖重写：现在在哪 / 下一步 3 件 / 卡点")
    return ok(f"{age_h:.1f} 小时前更新")


@check("DC-03", "文档", "INDEX 行号表与规格卡实际标题一致", gate=("W4", "W6", "W8"))
def index_linenos(ctx: Context) -> Result:
    idx = ctx.docs_dir / "INDEX.md"
    spec = _spec_file(ctx)
    if not idx.is_file() or spec is None:
        return skip("缺 INDEX.md 或规格卡")
    lines = ctx.read_text(spec).splitlines()
    rows = ROW.findall(ctx.read_text(idx))
    if not rows:
        return skip("INDEX 里没解析到行号表")
    bad = []
    for card, lineno in rows:
        n = int(lineno)
        head = card.split("/")[0]
        if n > len(lines):
            bad.append(f"{card}→{n}(越界)")
            continue
        target = lines[n - 1]
        if head not in target:
            bad.append(f"{card}→{n} 实为「{target.strip()[:24]}」")
    if bad:
        return fail(f"{len(bad)}/{len(rows)} 行号失效：{'; '.join(bad[:2])}",
                    "规格卡改版后必须重算：见 INDEX §四 的命令")
    return ok(f"{len(rows)} 条行号全部命中 {spec.name}")


@check("DC-04", "文档", "AGENTS.md 存在且含铁律/指路", gate=())
def agents_md(ctx: Context) -> Result:
    p = ctx.root / "AGENTS.md"
    if not p.is_file():
        return fail("缺 AGENTS.md", "每个会话的常驻上下文，缺了就要重新讲一遍项目")
    text = ctx.read_text(p)
    need = [k for k in ("铁律", "去哪找", "坑") if k not in text]
    if need:
        return partial(f"缺小节 {need}", "至少保留：铁律 / 目录地图 / 坑 / 指路")
    return ok(f"{len(text.splitlines())} 行")


@check("DC-05", "文档", "决策记录格式统一（每条含结论与理由）", gate=())
def decisions(ctx: Context) -> Result:
    d = ctx.docs_dir / "decisions"
    if not d.is_dir():
        return skip("没有 decisions 目录")
    files = [p for p in sorted(d.glob("DR-*.md"))]
    if not files:
        return skip("还没有决策记录")
    bad = [p.name for p in files
           if not all(k in ctx.read_text(p) for k in ("结论", "理由"))]
    if bad:
        return partial(f"{len(bad)}/{len(files)} 缺结论或理由：{bad[:2]}",
                       "模板末栏「没选的那条路为什么不行」是防重议的关键")
    return ok(f"{len(files)} 条，均含结论与理由")
