#!/usr/bin/env python3
"""打**定向**交付包：给 A 的、给 B 的，各自只带需要的部分。

与 `make_delivery.py` 的区别：那个打全仓库（给评审/存档看整体），这个按责任人切分
（给对方干活用）。为什么要切：

  1. A 只做检测，给他 lock 集等于把评测集递到他手里，违反「lock 不用于调参」的纪律；
  2. B 做集成，需要 lock 才能跑全链路，也需要 `ui/` 才能接 Decision；
  3. A 不需要 `tools/exiftool/`（他只做检测，不调 exiftool）；只有 B 的包带它——
     理由见下面 `TARGETS["B"]` 里的注释（2026-11-08 改：原来两包都不带）；
  4. 都给全量文档只会淹没人——每人只带与自己相关的那几份。

用法：
    python tools/make_handoff.py --to A
    python tools/make_handoff.py --to B
    python tools/make_handoff.py --to A --to B      # 两个都打

产物写 `dist/`，同时打印文件数、体积、SHA-256。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# 每个包带哪些目录（git 跟踪的文件）、哪些文档、用哪份交付说明
TARGETS = {
    "A": {
        "label": "C 交付给 A（检测引擎对接）",
        "dirs": ["requirements.txt", "contract", "dataset/dev", "eval", "tools"],
        # 实拍素材在 .gitignore 里（可能含真实照片，绝不入库），只能按盘上文件收。
        # 全部为虚构内容：9 张屏幕翻拍 + 5 张手写纸面。
        "extra_glob": ["dataset/real/*.jpg", "dataset/real/manifest.csv", "dataset/real/README.md"],
        "docs": [
            # 主文档放最前：它吸收了"同步给A"与"四点回复"的结论，并更正了 11-07 的耗时归因
            "docs/回A_2026-11-08_v2验收与耗时归因更正.md",
            "docs/timing-discipline.md",
            "docs/dev_chars_2026-11-08.json",
            "docs/AB接口与数据需求_2026-11-07.md",
            "docs/给A_任务单_2026-11-07.md",
            "docs/评测协议_v1.0.md",
            "docs/标注指南_R-01_v1.md",
            "docs/功能规格卡_v1.4.md",
        ],
        "readme": "docs/交付说明_A.md",
    },
    "B": {
        "label": "C 交付给 B（载入/处置/复检/导出对接）",
        "dirs": ["requirements.txt", "contract", "dataset", "eval", "ui", "tools"],
        # ExifTool 在 .gitignore 里（35 MB 免安装版），`git ls-files` 收不到，
        # 所以 11-07 那版包**实际没带**，只靠"对方环境已有"。
        # 2026-11-08 改：带上。B 要复验导出结果，exiftool 版本不同会让元数据输出不一致，
        # 而规格卡要求团队统一 13.61——"各自装一个"没法保证版本。包大 10 MB，值。
        "extra_glob": ["tools/exiftool"],
        "docs": [
            # 三份旧的（任务单/同步/四项阻塞）已被合并说明吸收，不再各带一份——
            # 同一个人拿四份文档、其中两份结论相反，是最容易出错的分发方式。
            # 11-08 的核实回应放最前：它更正了合并说明里的耗时结论与入口数
            "docs/回B_2026-11-08_核实回应与K03口径.md",
            "docs/timing-discipline.md",
            "docs/给B_合并说明_2026-11-07.md",
            "docs/AB接口与数据需求_2026-11-07.md",
            "docs/待三人同意清单_2026-11-07.md",
            "docs/风险分值对照与规格卡矛盾_2026-11-07.md",
            "docs/评测协议_v1.0.md",
            "docs/标注指南_R-01_v1.md",
            "docs/功能规格卡_v1.4.md",
        ],
        "readme": "docs/交付说明_B.md",
    },
}


def tracked_files(patterns: list[str]) -> list[Path]:
    """只取 git 已跟踪的文件——不会把 __pycache__ / .venv / results 混进来。

    **必须用 `-z` + `core.quotepath=false`**：默认情况下 `git ls-files` 会把含中文的路径
    转义成 `"docs/\\345\\217\\221..."` 这种带引号的八进制串，`Path()` 拿它当字面路径，
    文件当然不存在，于是被静默过滤掉。2026-11-07 踩过——交付包里整个 `docs/` 目录消失，
    而脚本不报错，只是文件数从 100 变成 101（README 还在，看起来很正常）。
    """
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z", *patterns],
        cwd=ROOT, capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout
    return [ROOT / p for p in out.split("\0") if p]


# 交付包里文档一律用英文名：对方解压后不会遇到编码问题，路径也能直接贴进命令。
# 仓库内仍保留中文名（INDEX 靠它索引、检索也好用），只在打包时改名。
DOC_ALIAS = {
    "docs/回A_2026-11-08_v2验收与耗时归因更正.md": "docs/reply-to-A-v2-and-timing.md",
    "docs/dev_chars_2026-11-08.json": "docs/dev-chars.json",
    "docs/AB接口与数据需求_2026-11-07.md": "docs/api-and-data-contract.md",
    "docs/评测协议_v1.0.md": "docs/eval-protocol.md",
    "docs/标注指南_R-01_v1.md": "docs/labeling-guide.md",
    "docs/功能规格卡_v1.4.md": "docs/spec-v1.4.md",
    "docs/交付说明_A.md": "docs/handoff-readme.md",
    "docs/交付说明_B.md": "docs/handoff-readme.md",
    "docs/给A_任务单_2026-11-07.md": "docs/tasks-for-A.md",
    "docs/回A_2026-11-07_四点回复与两处指正.md": "docs/reply-to-A-corrections.md",
    "docs/回A_2026-11-07_判据与耗时基线.md": "docs/reply-to-A-metric-and-timing.md",
    "docs/同步给A_2026-11-07.md": "docs/status-sync-A.md",
    "docs/回B_2026-11-08_核实回应与K03口径.md": "docs/reply-to-B-verification-and-K03.md",
    "docs/给B_合并说明_2026-11-07.md": "docs/handoff-notes-for-B.md",
    "docs/给B_任务单_2026-11-07.md": "docs/tasks-for-B.md",
    "docs/同步给B_2026-11-07.md": "docs/status-sync-B.md",
    "docs/待三人同意清单_2026-11-07.md": "docs/votes-pending.md",
    "docs/风险分值对照与规格卡矛盾_2026-11-07.md": "docs/risk-scores-and-spec-conflicts.md",
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(target: str) -> Path:
    spec = TARGETS[target]
    files = tracked_files(spec["dirs"] + spec["docs"])
    for pattern in spec.get("extra_glob", ()):
        # 变量名不能叫 target：那是 build() 的参数（"A"/"B"），
        # 覆盖之后包名会变成 `PhotoCheck_handoff_D://PhotoCheck//tools//exiftool_*.zip`。
        entry = ROOT / pattern
        if entry.is_dir():
            # 目录形式：递归收。`Path.glob("x/**")` 在这里不递归（实测返回 0），
            # 所以显式用 rglob，别指望 ** 的语义。
            matched = sorted(p for p in entry.rglob("*") if p.is_file())
        else:
            matched = sorted(p for p in ROOT.glob(pattern) if p.is_file())
        if not matched:
            # 静默漏装是这类脚本最坏的失败方式（文件数对不上才发现）
            raise SystemExit(f"extra_glob 没匹配到文件：{pattern}")
        files.extend(matched)
    readme = ROOT / spec["readme"]
    if not readme.is_file():
        raise SystemExit(f"缺交付说明：{spec['readme']}")
    files.append(readme)

    # 去重 + 排序，保证同样的 commit 打出同样的包
    seen: set[str] = set()
    unique: list[Path] = []
    for p in sorted(files):
        rel = p.relative_to(ROOT).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        if p.is_file():
            unique.append(p)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 顶层目录用纯 ASCII：zip 内文件名的 UTF-8 flag 是对的，但老版解压工具
    # （Git Bash 自带的 unzip 就是）不认这个 flag，会把中文目录名解成乱码。
    # 根目录名 ASCII 至少保证「打开就能看到东西」。
    top = f"PhotoCheck_handoff_{target}_{stamp}"
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{top}.zip"

    # 自检：`git ls-files` 只认已跟踪文件，新增但没 `git add` 的会被静默漏掉。
    # 2026-11-07 踩过两次——一次是中文路径被转义（docs 整个丢失），
    # 一次是新样本没 add（43 张图只装了 40 张）。两次都不报错，只是文件数少了。
    for rel_dir, pattern in (("dataset/dev/images", "*.jpg"), ("dataset/dev/labels", "*.json")):
        on_disk = len(list((ROOT / rel_dir).glob(pattern)))
        # 前缀必须带结尾斜杠：否则 `dataset/dev/labels.csv` 会被算进 `dataset/dev/labels/`，
        # 报出「目录 38 个、包里 39 个」这种看起来像多装了的假告警。
        prefix = (ROOT / rel_dir).as_posix() + "/"
        packed = len([p for p in unique if p.as_posix().startswith(prefix)])
        if packed != on_disk:
            raise SystemExit(
                f"[自检失败] {rel_dir}: 目录里 {on_disk} 个 {pattern}，包里只有 {packed} 个。\n"
                f"          大概率是新增文件还没 `git add`（git ls-files 看不到未跟踪文件）。"
            )

    rows: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in unique:
            rel = p.relative_to(ROOT).as_posix()
            if p == readme:
                arcname = f"{top}/README.md"          # 交付说明固定放包根
            else:
                # 文档按 DOC_ALIAS 改英文名；代码与数据保持原路径
                arcname = f"{top}/{DOC_ALIAS.get(rel, rel)}"
            zf.write(p, arcname)
            rows.append(f"{sha256_of(p)}  {arcname}")

        manifest = "\n".join([
            f"# {spec['label']}",
            f"# 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
            f"　提交：{_git('rev-parse', '--short', 'HEAD')}"
            f"{'（工作区有未提交改动）' if _git('status', '--porcelain') else ''}",
            f"# 文件数：{len(unique)}",
            "#",
            "# 校验：sha256sum -c MANIFEST.sha256（或在 PowerShell 里逐行比对）",
            "",
            *rows,
            "",
        ])
        zf.writestr(f"{top}/MANIFEST.sha256", manifest)

    size_mb = out.stat().st_size / 1024 / 1024
    # 包外再放一份 `.zip.sha256`：A 明确要求过——只把哈希写在文档里，
    # 收到包的人没法验证"文档里的哈希说的是不是这个包"。
    digest = sha256_of(out)
    (DIST / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(f"  {spec['label']}")
    print(f"    {out.name}　{len(unique)} 文件　{size_mb:.1f} MB　sha256 {digest[:16]}…")
    return out


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打定向交付包（给 A / 给 B）")
    ap.add_argument("--to", action="append", choices=sorted(TARGETS), required=True,
                    help="打给谁；可重复")
    args = ap.parse_args(argv)

    if _git("status", "--porcelain"):
        print("[警告] 工作区有未提交改动——包内文件是当前工作区状态，不是某个确定的提交")

    print(f"打交付包（{datetime.now():%Y-%m-%d %H:%M}）")
    outs = [build(t) for t in dict.fromkeys(args.to)]
    print("\n完成：")
    for o in outs:
        print(f"  {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
