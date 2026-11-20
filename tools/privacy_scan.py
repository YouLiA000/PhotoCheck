#!/usr/bin/env python3
"""隐私扫描：产物里不许出现完整手机号 / 身份证号（§0.5 红线 1）。

两个用途：
  - `python tools/privacy_scan.py`           扫当前代码树（白名单外全部文本）
  - `python tools/privacy_scan.py --staged`  扫**本次暂存的文件**，给 pre-commit 用

为什么要有 --staged 版：审计里的 PV-02 是**事后**检查——等它报出来，文件已经在仓库里了，
git 历史洗不掉。红线类问题必须在提交那一刻拦住。

合成样本与测试夹具里出现号码是**正常的**（那本来就是被测对象），所以有一份显式白名单。
白名单之外的任何一处命中都算失败，不区分"看起来像真的"还是"看起来像假的"——
扫描器没有能力判断真伪，声称能判断的都是在给自己开后门。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contract.masking import mask_for  # noqa: E402

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

TEXT_EXT = {".md", ".txt", ".csv", ".json", ".py", ".html", ".yaml", ".yml", ".log"}

# 遍历时跳过的目录：第三方依赖里有大量示例号码，扫它们没有意义
SKIP_DIRS = {
    ".venv", "venv", ".git", "node_modules", "__pycache__",
    "build", "dist", "work", "copy", "tmp",
}

# 允许出现完整号码的地方，逐条写清理由。**不要往里加"临时"条目**——
# 白名单一旦变成垃圾桶，这个检查就只剩心理安慰。
ALLOW_PREFIXES = (
    "dataset/dev/",        # 合成样本与其真值：号码就是被测对象
    "dataset/lock/",       # 锁定集同理，且已按字节封存
    "dataset/tools/",      # 生成器与测试夹具
    "contract/tests/",     # 构造用例里的号码
    "contract/examples/",  # 契约示例（掩码为主，含少量构造值）
    "photocheck/tests/",   # B 的构造图片、契约夹具与负例
    "photocheck/contract/tests/",
    "photocheck/detectors/a_engine/tests/",
    "photocheck/detectors/a_engine/scripts/",   # 合成样本生成器中的固定测试号码
    "photocheck/detectors/a_engine/badcases/",  # 失败案例中的最小复现
    "eval/tests/",
    "ui/tests/",
    "tools/tests/",        # 隐私扫描自身的构造号码
    "tools/exiftool/",     # 第三方工具源码
    ".workbuddy/",         # 会话记忆，不入库
    "docs/规格卡历史/",     # 团队历史文档，只读
)

# 产物目录：这些地方一律严格扫，不受白名单影响
ARTIFACT_DIRS = ("results", "reports")


def _in_artifact(rel: str) -> bool:
    return any(rel == name or rel.startswith(name + "/") for name in ARTIFACT_DIRS)


def _is_allowed(rel: str) -> bool:
    prefixes = ALLOW_PREFIXES + tuple(
        "app/vendor/" + prefix
        for prefix in ALLOW_PREFIXES
        if prefix.startswith("photocheck/")
    )
    return any(rel.startswith(p) for p in prefixes)


def _iter_files(base: Path) -> list[Path]:
    """遍历时跳过点目录与构建缓存，避免把第三方/临时内容当成产物。"""
    files = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(base).parts
        if any(part.startswith(".") for part in parts):
            continue
        if set(path.parts) & SKIP_DIRS:
            continue
        files.append(path)
    return files


def _scan_text(text: str) -> list[tuple[int, str, str]]:
    """一行里有几处就报几处。

    早先用 `search` 只报第一处：一行塞了两个号码时，报告只显示一个，
    容易让人以为"改完这一个就干净了"。红线工具低报比误报危险。
    """
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pat in (("phone", PHONE), ("idcard", IDCARD)):
            for match in pat.finditer(line):
                hits.append((lineno, name, mask_for("text", name, match.group(0))))
    return hits


def _staged_files() -> list[Path]:
    # 必须用 -z：git 默认会把非 ASCII 文件名转义成 "æ ..."，
    # 拼出来是路径不存在的假名字，扫描器会静默扫 0 个文件（已踩过一次）。
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        cwd=str(ROOT), capture_output=True,
    )
    if out.returncode != 0:
        detail = out.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"读取暂存文件失败（git 退出码 {out.returncode}）：{detail}")
    names = out.stdout.decode("utf-8", "replace").split("\0")
    return [ROOT / n for n in names if n.strip()]


def _artifact_files() -> list[Path]:
    files: list[Path] = []
    for name in ARTIFACT_DIRS:
        base = ROOT / name
        if base.is_dir():
            files += [p for p in _iter_files(base) if p.suffix.lower() in TEXT_EXT]
    return files


def _repository_files() -> list[Path]:
    return [
        p for p in _iter_files(ROOT)
        if p.suffix.lower() in TEXT_EXT
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="隐私扫描：禁止完整手机号/身份证号进入产物")
    ap.add_argument("--staged", action="store_true", help="只扫本次暂存的文件（pre-commit 用）")
    ap.add_argument("paths", nargs="*", help="指定文件或目录（默认扫产物目录）")
    args = ap.parse_args(argv)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    elif args.staged:
        try:
            targets = _staged_files()
        except RuntimeError as exc:
            print(f"隐私扫描失败：{exc}", file=sys.stderr)
            return 2
    else:
        targets = _repository_files()

    findings = []
    scan_errors: list[str] = []
    scanned = 0
    for path in targets:
        if not path.exists():
            scan_errors.append(f"路径不存在：{path}")
            continue
        files = _iter_files(path) if path.is_dir() else [path]
        for f in files:
            if f.suffix.lower() not in TEXT_EXT:
                continue
            try:
                rel = f.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                rel = f.name
            # 产物目录永远严格扫，白名单只对源码/文档生效
            if not _in_artifact(rel) and _is_allowed(rel):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                scan_errors.append(f"无法读取 {rel}：{type(exc).__name__}")
                continue
            scanned += 1
            for lineno, name, masked in _scan_text(text):
                findings.append((rel, lineno, name, masked))

    if findings:
        print(f"发现 {len(findings)} 处完整号码（扫描 {scanned} 个文件）：")
        for rel, lineno, name, masked in findings[:20]:
            print(f"  {rel}:{lineno}  {name}  {masked}")
        if len(findings) > 20:
            print(f"  …… 另有 {len(findings) - 20} 处")
        print("\n产物里只允许掩码（如 138****0000）。掩码统一走 contract/masking.py，不要手拼。")

    if scan_errors:
        print(f"隐私扫描未完成：{len(scan_errors)} 个文件/路径无法检查", file=sys.stderr)
        for error in scan_errors[:20]:
            print(f"  {error}", file=sys.stderr)
        return 2
    if findings:
        return 1
    print(f"隐私扫描通过：{scanned} 个文件，未发现完整手机号 / 身份证号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
