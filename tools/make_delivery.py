#!/usr/bin/env python3
"""打交付包：从某个提交导出**已跟踪文件**，再补进不进 git 的免安装工具。

为什么不用双击压缩：交付包必须满足三条，手工做每次都会漏一条——
  1. **只含已跟踪文件**（`git archive`），不会把 .venv / results / __pycache__ 混进去；
  2. **补上 git 里没有但运行时需要的东西**（ExifTool 体积大、按 .gitignore 不入库）；
  3. **记录 SHA-256**，且哈希写在包外（包内放自身哈希会自指）。

用法：
    python tools/make_delivery.py                     # 默认：HEAD + tools/exiftool
    python tools/make_delivery.py --rev v1.0-final    # 按标签打包
    python tools/make_delivery.py --no-extra          # 不补免安装工具（对方自己装）

工作区有未提交改动时**默认拒绝**：交付包必须对应一个确定的提交，否则对方复现不出来。
`--allow-dirty` 只表示"知情并允许按指定提交继续打包"，**不会把未提交改动装进包**；
包名会带 `-dirty`，提醒接收方工作区与提交并不一致。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# 不入库、但交付时希望一起带上的目录（相对项目根）
EXTRA_DIRS = ("tools/exiftool",)
# 这些永远不进包，防止误加
NEVER = {".venv", ".git", "__pycache__", "results", "dist", "build"}


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout.strip()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_extras(extras: list[Path]) -> list[tuple[str, Path]]:
    """(包内路径, 磁盘路径)。跳过不存在目录与永远不该进包的路径。"""
    out: list[tuple[str, Path]] = []
    for extra in extras:
        base = ROOT / extra
        if not base.is_dir():
            print(f"  跳过不存在的额外目录：{extra}", file=sys.stderr)
            continue
        added = 0
        for path in sorted(base.rglob("*")):
            if not path.is_file() or set(path.parts) & NEVER:
                continue
            out.append(((Path("PhotoCheck") / path.relative_to(ROOT)).as_posix(), path))
            added += 1
        print(f"  额外打入 {extra}/：{added} 个文件")
    return out


def build_zip(rev: str, commit_ts: int, out: Path, extras: list[tuple[str, Path]]) -> None:
    stamp = time.localtime(commit_ts)[:6]
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base.zip"
        subprocess.run(["git", "archive", "--format=zip", "--prefix=PhotoCheck/",
                        "-o", str(base), rev], cwd=ROOT, check=True)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(
            out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                # mtime 用**提交时间**：同一提交重复打包字节一致（可复现），
                # 又不会像固定成 2026-01-01 那样让"文件多久没更新"的检查误判。
                fixed = zipfile.ZipInfo(info.filename, date_time=stamp)
                fixed.compress_type = zipfile.ZIP_DEFLATED
                fixed.external_attr = 0o644 << 16
                dst.writestr(fixed, data)
            for arc, path in extras:
                fixed = zipfile.ZipInfo(arc, date_time=stamp)
                fixed.compress_type = zipfile.ZIP_DEFLATED
                fixed.external_attr = 0o644 << 16
                dst.writestr(fixed, path.read_bytes())


def build_tar(rev: str, commit_ts: int, out: Path, extras: list[tuple[str, Path]]) -> None:
    """同时出一个 tar.gz：`unzip`（Info-ZIP）在 Windows 上会把 UTF-8 文件名当 CP437，
    解出乱码；tar 存的是原始字节，不受区域设置影响。多一个包，少一类"打开就乱码"的事故。
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base.tar"
        # --prefix 必须和 zip 一样：少了它，仓库文件解到根目录、附加目录却在 PhotoCheck/ 下，
        # 解压出来是两套东西叠在一起
        subprocess.run(["git", "archive", "--format=tar", "--prefix=PhotoCheck/",
                        "-o", str(base), rev], cwd=ROOT, check=True)
        with tarfile.open(base) as src, tarfile.open(out, "w:gz") as dst:
            for member in src.getmembers():
                member.mtime = commit_ts
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                fh = src.extractfile(member) if member.isfile() else None
                dst.addfile(member, fh)
            for arc, path in extras:
                info = tarfile.TarInfo(arc)
                info.size = path.stat().st_size
                info.mtime = commit_ts
                info.mode = 0o644
                with open(path, "rb") as fh:
                    dst.addfile(info, fh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打 PhotoCheck 交付包")
    ap.add_argument("--rev", default="HEAD", help="提交 / 标签，默认 HEAD")
    ap.add_argument("--out", default=None,
                    help="zip 输出路径，默认 dist/PhotoCheck_<日期>.zip；同名 .tar.gz 一并生成")
    ap.add_argument("--extra", nargs="*", default=list(EXTRA_DIRS),
                    help="额外打包的目录（不跟踪也要带上），默认 tools/exiftool")
    ap.add_argument("--no-extra", action="store_true", help="不补任何额外目录")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="知情并允许按指定提交打包；未提交改动不会进入包内")
    args = ap.parse_args(argv)

    dirty = bool(git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        print("工作区有未提交改动，交付包会与提交不一致。\n"
              "  先提交，或加 --allow-dirty（未提交改动仍不会进入包内，包名会带 -dirty）。",
              file=sys.stderr)
        return 2
    if dirty:
        print("注意：工作区有未提交改动；本包只含指定提交，未提交内容不会被打入。",
              file=sys.stderr)

    rev = args.rev
    # 轻量/附注标签都要落到提交上：`git rev-parse v1.0` 对附注标签返回的是**标签对象**的
    # 哈希（25030ea…），写进交付说明会让人以为那是提交，回头查不到任何代码。
    commit = git("rev-parse", f"{rev}^{{commit}}")
    short = commit[:7]
    tag = git("describe", "--tags", "--always", rev) if rev in ("HEAD", "") else rev
    commit_ts = int(git("log", "-1", "--format=%ct", commit))
    stamp_day = datetime.now().strftime("%Y-%m-%d")
    suffix = "-dirty" if dirty else ""
    out = Path(args.out) if args.out else DIST / f"PhotoCheck_{stamp_day}{suffix}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    extras = _collect_extras([] if args.no_extra else [Path(p) for p in args.extra])

    build_zip(rev, commit_ts, out, extras)
    tar = out.with_suffix(".tar.gz")
    build_tar(rev, commit_ts, tar, extras)

    lines = []
    for path in (out, tar):
        digest = sha256_of(path)
        lines.append(f"{digest} *{path.name}")
        # zip 的 Info 与 tar 的 TarInfo 接口不同（is_dir vs isdir），分开数
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                count = sum(1 for i in z.infolist() if not i.is_dir())
        else:
            with tarfile.open(path) as t:
                count = sum(1 for m in t.getmembers() if m.isfile())
        print(f"\n交付包  {path}")
        print(f"  文件    {count} 个，{path.stat().st_size / 1024 / 1024:.1f} MB")
        print(f"  SHA-256 {digest}")
    sums = out.with_name("SHA256.txt")
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    print(f"\n  提交    {short}（{tag}）{'  [!] 工作区有未提交改动（未打入）' if dirty else ''}")
    print(f"  校验值  {sums.name}（与包同级，两行分别对应 zip / tar.gz）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
