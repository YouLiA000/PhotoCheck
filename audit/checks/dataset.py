"""数据层检查（K-13 / R-01）：张数、真值完整性、lock 隔离、配额。

这里的几条「隔离」检查是有来历的：lock 集第一版的 image_id 是 image-dev-001..020，
与 dev 完全重名。契约只校验「同一 image_id 内唯一」，所以 validate_records 一条都不报，
但评测按 image_id 索引时会串图。**静默失败只有这种断言能抓住**，所以它们是永久检查项。
"""

from __future__ import annotations

import json
from collections import Counter

from audit.model import Context, Result, check, fail, ok, partial, skip

DEV_MIN = 40
LOCK_MIN = 20
LOCK_CLEAN_MIN = 4

KIND_QUOTA = {"qr": 5, "phone": 3, "idcard": 3}


def _broken_ids(ctx: Context) -> set[str]:
    """坏图（template=broken）按约定不配真值、也不要求能打开。

    之前按文件名里有没有 "broken" 猜，是错的——文件名是 image-dev-027 这种，
    猜不出来，于是把 5 张故意做坏的图误报成了「缺真值 + 图损坏」。
    """
    return {r.get("image_id") for r in ctx.manifest("dev")
            if r.get("template") == "broken" and r.get("image_id")}


@check("DS-01", "K-13", f"dev 集 ≥{DEV_MIN} 张", gate=("W4",))
def dev_count(ctx: Context) -> Result:
    n = len(list((ctx.dev_dir / "images").glob("*.jpg")))
    return ok(f"{n} 张") if n >= DEV_MIN else fail(f"{n}/{DEV_MIN}", "在 gen_samples.py 的 SPECS 追加条目")


@check("DS-02", "K-13", "dev 图与真值一一对应（坏图允许缺）", gate=("W4",))
def dev_label_cover(ctx: Context) -> Result:
    imgs = {p.stem for p in (ctx.dev_dir / "images").glob("*.jpg")}
    lbls = set(ctx.labels("dev"))
    broken = _broken_ids(ctx) & imgs
    missing = imgs - lbls
    orphan = lbls - imgs
    if orphan:
        return fail(f"{len(orphan)} 份真值没有对应图片", "删掉孤儿真值或补图")
    unexplained = missing - broken
    if unexplained:
        return fail(f"{len(unexplained)} 张正常图缺真值：{sorted(unexplained)[:3]}",
                    "正常图必须有真值；坏图才允许缺")
    if missing:
        return ok(f"{len(imgs)} 图 / {len(lbls)} 真值，{len(missing)} 张坏图按约定无真值")
    return ok(f"{len(imgs)} 图 / {len(lbls)} 真值，全覆盖")


@check("DS-03", "K-13", "dev 真值全部通过契约校验", gate=("W4",))
def dev_contract(ctx: Context) -> Result:
    import subprocess
    import sys

    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = __import__("pathlib").Path(sys.executable)
    bad = []
    files = sorted((ctx.dev_dir / "labels").glob("*.json"))
    for p in files:
        proc = subprocess.run([str(python), "contract/validate_records.py", str(p)],
                              cwd=str(ctx.root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            bad.append(p.name)
    if bad:
        return fail(f"{len(bad)}/{len(files)} 份不合法：{', '.join(bad[:3])}", "跑单份看具体报错")
    return ok(f"{len(files)}/{len(files)} 份通过")


@check("DS-04", "K-13", f"lock 集 ≥{LOCK_MIN} 张", gate=("W6",))
def lock_count(ctx: Context) -> Result:
    """K-13 原文是「lock ≥20 张」「合计 ≥60 张」，不是"正好 20"。

    原来写 `== LOCK_MIN` 是为了防止随手加图，但它会误伤正当补充：干净图要 4 张、
    模板又必须与 dev 零交集，补到 23 张是配额算出来的结果，不是手滑。
    多出来有没有害由 DS-06（模板不相交）、DS-09（配额）把关，这里只管下限。
    """
    n = len(list((ctx.lock_dir / "images").glob("*.jpg")))
    if n < LOCK_MIN:
        return fail(f"{n}/{LOCK_MIN}", "补条目后重生成 + 重封")
    return ok(f"{n} 张")


@check("DS-05", "K-13", "lock 已封存且未被改动", gate=("W6",))
def lock_sealed(ctx: Context) -> Result:
    import subprocess
    import sys

    seal = ctx.lock_dir / "SEALED.json"
    sums = ctx.lock_dir / "SHA256SUMS.txt"
    if not seal.exists() or not sums.exists():
        return fail("无 SEALED.json / SHA256SUMS.txt", "python dataset/tools/seal_lock.py seal")
    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = __import__("pathlib").Path(sys.executable)
    proc = subprocess.run([str(python), "dataset/tools/seal_lock.py", "verify"],
                          cwd=str(ctx.root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return fail((proc.stdout or proc.stderr).strip()[:160], "被改过就是被污染，整体重生成 + 重封")
    try:
        meta = json.loads(seal.read_text(encoding="utf-8"))
        when = meta.get("sealed_at", "?")
    except Exception:
        when = "?"
    return ok(f"{meta.get('image_count', '?')} 张，封存于 {when}")


@check("DS-06", "K-13", "dev 与 lock 的 image_id 零交集（防串图）", gate=("W6",))
def id_disjoint(ctx: Context) -> Result:
    dev, lock = set(ctx.labels("dev")), set(ctx.labels("lock"))
    if not dev or not lock:
        return skip("有一侧读不到真值", "先确认 labels 目录")
    inter = dev & lock
    if inter:
        return fail(f"{len(inter)} 个 ID 重名，如 {sorted(inter)[:3]}",
                    "契约只查单图内唯一，重名不会报错但评测会串图；重新生成 lock")
    prefixes = {i.split("-")[1] for i in lock if i.count("-") >= 2}
    return ok(f"dev {len(dev)} / lock {len(lock)} 零交集，lock 前缀 {prefixes or '?'}")


@check("DS-07", "K-13", "dev 与 lock 模板零交集（防模板泄漏）", gate=("W6",))
def template_disjoint(ctx: Context) -> Result:
    d = {r.get("template") for r in ctx.manifest("dev")} - {None, ""}
    l = {r.get("template") for r in ctx.manifest("lock")} - {None, ""}
    if not d or not l:
        return skip("有一侧没有 manifest", "确认 dataset/*/manifest.csv")
    inter = d & l
    if inter:
        return fail(f"共用模板 {sorted(inter)}", "同模板进了两边 = 模板泄漏，lock 作废")
    return ok(f"dev {sorted(d)} / lock {sorted(l)}")


@check("DS-08", "K-13", f"lock 干净图 ≥{LOCK_CLEAN_MIN} 张（误报率来源）", gate=("W6",))
def lock_clean(ctx: Context) -> Result:
    rows = ctx.manifest("lock")
    if not rows:
        return skip("没有 lock manifest")
    clean = [r for r in rows if int(r.get("positives", 0)) == 0 and int(r.get("negatives", 0)) == 0]
    n = len(clean)
    if n >= LOCK_CLEAN_MIN:
        return ok(f"{n} 张")
    return fail(f"{n}/{LOCK_CLEAN_MIN}",
                "LOCK_SPECS 加不带 qr/phone/idcard/neg 的纯版式条目，重生成后重封；"
                "必须在 eval.py 首次跑 lock 前补完")


@check("DS-09", "K-13", "lock 各类型配额达标", gate=("W6",))
def lock_quota(ctx: Context) -> Result:
    # 二维码按 kind 计，手机号/身份证按 subtype 计——它们的 kind 都是 text，
    # 按 subtype 统一取会把 qr 计成 0（qr 的 subtype 是 qr_link / qr_wifi 这类）。
    kinds, subtypes = Counter(), Counter()
    for records in ctx.labels("lock").values():
        for r in records:
            if r.get("record_type") == "Detection":
                kinds[r.get("kind")] += 1
                if r.get("subtype"):
                    subtypes[r["subtype"]] += 1
    got = {"qr": kinds["qr"], "phone": subtypes["phone"], "idcard": subtypes["idcard"]}
    detail = " / ".join(f"{k} {got.get(k, 0)}≥{v}" for k, v in KIND_QUOTA.items())
    short = {k: v for k, v in KIND_QUOTA.items() if got.get(k, 0) < v}
    if short:
        return partial(detail, f"缺 {short}")
    return ok(detail)


@check("DS-10", "K-13", "所有样本图可正常打开", gate=("W4", "W6"))
def images_readable(ctx: Context) -> Result:
    try:
        from PIL import Image
    except ImportError:
        return skip("PIL 不可用", "确认 .venv 装齐 requirements")
    bad, total, skipped = [], 0, 0
    broken = _broken_ids(ctx)
    for d in (ctx.dev_dir / "images", ctx.lock_dir / "images"):
        for p in sorted(d.glob("*.jpg")):
            if p.stem in broken:  # 故意做坏的图，打不开才是对的
                skipped += 1
                continue
            total += 1
            if p.stat().st_size == 0:
                bad.append(f"{p.name}(0字节)")
                continue
            try:
                with Image.open(p) as im:
                    im.verify()
            except Exception:
                bad.append(p.name)
    if bad:
        return fail(f"{len(bad)}/{total} 张损坏：{', '.join(bad[:3])}", "重生成对应样本")
    extra = f"，跳过 {skipped} 张故意坏图" if skipped else ""
    return ok(f"{total} 张正常图全部可打开{extra}")


@check("DS-10", "K-13", "lock 目录禁换行转换（-text）", gate=("W6",))
def lock_no_eol_conversion(ctx: Context) -> Result:
    """封存按字节算哈希，git 只要做一次 CRLF→LF 转换，封存就废了。

    2026-11-02 真踩过：seal 写 CRLF、checkout 按 `*.txt eol=lf` 改成 LF，
    `sums_sha256` 对不上，W6 直接判不过。**本地一切正常，只有重新检出才爆**，
    所以这条必须由审计盯着，不能靠记性。
    """
    import subprocess

    paths = [
        "dataset/lock/SHA256SUMS.txt",
        "dataset/lock/SEALED.json",
        "dataset/lock/labels/image-lock-001.json",
        "dataset/lock/manifest.csv",
    ]
    if not (ctx.root / paths[0]).exists():
        return skip("lock 集还没生成")

    try:
        proc = subprocess.run(
            ["git", "check-attr", "text", "--", *paths],
            cwd=str(ctx.root), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, OSError):
        return _no_eol_rule_static(ctx)

    # 不是 git 仓库（例如对方拿到的是 zip 而不是 clone）时降级为静态解析：
    # 这条检查守的是"规则在不在"，不该因为 `.git` 不在就整条跳过——
    # 交付包解压出来正好是这种情况，一跳过就等于没查。
    if proc.returncode != 0:
        return _no_eol_rule_static(ctx)

    rows = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    bad = [ln.rsplit(": ", 1)[0] for ln in rows if not ln.rstrip().endswith(": unset")]
    if bad:
        return fail(f"{len(bad)}/{len(rows)} 个路径不是 -text",
                    "在 .gitattributes 加 dataset/lock/** -text，否则下次检出封存就失效")
    return ok(f"{len(rows)} 个路径均为 -text")


def _no_eol_rule_static(ctx: Context) -> Result:
    """没有 git 时的兜底：直接看 .gitattributes 里有没有覆盖 lock 的 `-text` 规则。"""
    attrs = ctx.root / ".gitattributes"
    if not attrs.exists():
        return fail("没有 .gitattributes", "加一行 dataset/lock/** -text，否则封存会随换行失效")
    rules = []
    for line in attrs.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line.split()[0].startswith("dataset/lock"):
            rules.append(line)
    if not any("-text" in rule.split()[1:] for rule in rules):
        return fail(f"lock 规则 {rules or '缺失'} 未声明 -text",
                    "换成 dataset/lock/** -text；否则换行规范化会让封存哈希失效")
    return ok(f"静态解析 .gitattributes：{rules[0]}（无 .git，未做 check-attr）")
