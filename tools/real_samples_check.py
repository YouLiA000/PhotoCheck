#!/usr/bin/env python3
"""检查 dataset/real/ 里的授权真实照片。

这个脚本存在的**唯一理由**是防止照片被误提交进 git。其它检查（能不能打开、字段全不全）
都是次要的——图打不开改个文件名就行，照片进了 git 历史就删不干净了。

三道检查，按重要性排：

1. **照片没有被 git 跟踪**（最重要）。`dataset/real/` 在 .gitignore 里，但只要有人
   执行过 `git add -f`，或者 .gitignore 被改坏，照片就会进历史。这一步查的就是这个。
2. **manifest 的 consent 列填了**。没有授权来源的照片不能用——答辩被问"这张哪来的"
   答不上来，比指标低十分严重。
3. 文件存在、能打开、尺寸正常。

用法：
    python tools/real_samples_check.py            # 检查
    python tools/real_samples_check.py --seal     # 另外记下每张的 SHA-256
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "dataset" / "real"
MANIFEST = REAL / "manifest.csv"
# 这两个是说明文件，本来就该入库
ALLOWED_TRACKED = {"README.md", "manifest.example.csv", "manifest.csv"}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def check_not_tracked() -> list[str]:
    """返回被 git 跟踪的多媒体文件——空列表才算安全。"""
    out = git("ls-files", "dataset/real")
    leaked = []
    for line in out.splitlines():
        rel = line.strip()
        if not rel:
            continue
        name = Path(rel).name
        if name in ALLOWED_TRACKED:
            continue
        leaked.append(rel)
    return leaked


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查授权真实照片目录")
    ap.add_argument("--seal", action="store_true", help="记录每张图的 SHA-256")
    args = ap.parse_args(argv)

    problems: list[str] = []

    # ── 检查 1：有没有被 git 跟踪 ──
    leaked = check_not_tracked()
    if leaked:
        print("[危险] 以下照片已被 git 跟踪，必须立刻移出：")
        for p in leaked:
            print(f"        {p}")
        print("       修法：git rm --cached <文件>  然后确认 .gitignore 里的 dataset/real/ 还在")
        problems.append("照片被 git 跟踪")
    else:
        print("[OK] 没有照片被 git 跟踪")

    # ── 检查 2/3：manifest 与文件 ──
    if not MANIFEST.is_file():
        print(f"[提示] 还没有 {MANIFEST.relative_to(ROOT).as_posix()}")
        print("       从 manifest.example.csv 复制一份开始填：")
        print(f"       copy {MANIFEST.with_name('manifest.example.csv').relative_to(ROOT)} "
              f"{MANIFEST.relative_to(ROOT)}")
        return 1 if problems else 0

    with MANIFEST.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        print("[提示] manifest.csv 是空的")
        return 1 if problems else 0

    print(f"\n共 {len(rows)} 条记录：")
    print(f"  {'文件':<18} {'授权':<14} {'尺寸':<12} {'对象':<20} 状态")
    print("  " + "-" * 78)
    seal: dict[str, str] = {}

    for row in rows:
        name = (row.get("file") or "").strip()
        consent = (row.get("consent") or "").strip()
        objects = (row.get("objects") or "").strip()
        status: list[str] = []

        path = REAL / name
        if not name:
            print(f"  {'(空)':<18} {'-':<14} {'-':<12} {'-':<20} 文件名为空")
            problems.append("manifest 里有空文件名")
            continue
        if not path.is_file():
            print(f"  {name:<18} {consent or '-':<14} {'-':<12} {objects or '-':<20} 文件不存在")
            problems.append(f"{name} 不存在")
            continue
        if consent not in ("self", "team-written", "synthetic-photo"):
            status.append("授权列无效")
            problems.append(f"{name} 的 consent={consent!r} 不在允许值内")
        try:
            from PIL import Image
            with Image.open(path) as im:
                size = f"{im.width}×{im.height}"
                im.verify()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<18} {consent or '-':<14} {'-':<12} {objects or '-':<20} "
                  f"打不开：{type(exc).__name__}")
            problems.append(f"{name} 打不开")
            continue
        if args.seal:
            seal[name] = sha256_of(path)
        print(f"  {name:<18} {consent or '-':<14} {size:<12} {objects or '-':<20} "
              f"{'、'.join(status) or 'OK'}")

    if args.seal and seal:
        out = REAL / "SEALED.json"
        out.write_text(
            json.dumps({"sealed_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                        "count": len(seal), "files": seal},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        print(f"\n已记录 {len(seal)} 个哈希 → {out.relative_to(ROOT).as_posix()}")

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全部通过。真实照片不进 git、授权齐全、文件可读。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
