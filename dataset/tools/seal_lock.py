#!/usr/bin/env python3
"""lock 集封存与校验。

K-13 要求「lock 集封存并记录哈希」。封存的意义不是"存一份哈希留个念"，而是让**改动必定被发现**：
只要图片被替换、重生成或误改，`verify` 就会报出来。

    python dataset/tools/seal_lock.py seal      # 生成 SHA256SUMS.txt + SEALED.json
    python dataset/tools/seal_lock.py verify    # 逐个比对，返回码非 0 表示已被改动
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "dataset" / "lock"
SUMS_NAME = "SHA256SUMS.txt"
SEAL_NAME = "SEALED.json"


def sums_path(lock_dir: Path) -> Path:
    """封存记录路径**跟着目录走**。

    这里踩过一个静默错：模块级常量 `SUMS = LOCK / ...` 被 `seal(lock_dir)` / `verify(lock_dir)`
    直接使用，于是对任意目录封存都会覆盖真实 lock 的封存记录，而对任意目录校验其实是在
    校验真实 lock。函数签名说"支持传目录"，实现却不认——这种"签名与实现不一致"比不写参数更糟。
    """
    return lock_dir / SUMS_NAME


def seal_path(lock_dir: Path) -> Path:
    return lock_dir / SEAL_NAME



def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(lock_dir: Path) -> list[tuple[str, str]]:
    """返回 [(相对路径, sha256)]，路径统一用 / 分隔，跨平台可比。

    **图片和标注都要算**。只哈希图片等于只锁住半套题：真值文件被改一个字符，
    检测器照样跑、评测照样出分，但分数已经不是原来那套题的了——这种污染比换图更隐蔽。

    2026-11-20 修正：改为**递归全量枚举**。原实现按 `images/*.jpg` + `labels/*.json`
    + 三个固定根文件的白名单收集，白名单之外的新增（`*.png`、子目录里的杂项文件）
    根本进不了 `actual`，于是 `verify` 的「封存后新增」永远检测不到——
    lock 集「改动必被发现」的承诺形同虚设，而这正是铁律 6 依赖的机制。
    """
    entries: list[tuple[str, str]] = []
    self_names = {SUMS_NAME, SEAL_NAME}
    for path in sorted(lock_dir.rglob("*")):
        if not path.is_file():
            continue
        # 只排除 lock 根下的封存产物本身（子目录里的同名文件不放过）
        if path.parent == lock_dir and path.name in self_names:
            continue
        rel = path.relative_to(lock_dir).as_posix()
        entries.append((rel, sha256_of(path)))
    return entries


def seal(lock_dir: Path = LOCK) -> dict:
    entries = collect(lock_dir)
    sums = sums_path(lock_dir)
    # newline="\n"：不写死的话 Windows 会写成 CRLF，同一份数据在两台机器上哈希不同
    sums.write_text(
        "".join(f"{digest}  {rel}\n" for rel, digest in entries),
        encoding="utf-8", newline="\n",
    )
    labels = sorted((lock_dir / "labels").glob("*.json"))
    meta = {
        "sealed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "image_count": sum(1 for rel, _ in entries if rel.startswith("images/")),
        "label_count": len(labels),
        "file_count": len(entries),
        "sums_sha256": sha256_of(sums),
        "note": "锁定集只在 W8 / W20 / 答辩前各跑一次；平时不参与调参",
    }
    seal_path(lock_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8", newline="\n")
    return meta


def verify(lock_dir: Path = LOCK) -> list[str]:
    """返回问题列表，空表示完好。"""
    sums = sums_path(lock_dir)
    seal = seal_path(lock_dir)
    if not sums.exists():
        return [f"没有封存记录：{sums}"]
    problems: list[str] = []
    recorded = {}
    for lineno, line in enumerate(sums.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            # 记录本身格式坏了也要报出来，不能当成"没有这一行"悄悄放过
            problems.append(f"{sums.name}:{lineno} 格式不合法（应为 <64位哈希><两个空格><路径>）")
            continue
        digest, rel = parts
        if rel in recorded:
            # 重复行不能静默覆盖：两份不同的哈希指向同一文件时，
            # 谁在后面谁生效，等于让封存记录可以被"追加一行"改写
            problems.append(f"{sums.name}:{lineno} 重复记录：{rel}")
            continue
        recorded[rel] = digest

    actual = {rel: digest for rel, digest in collect(lock_dir)}
    for rel, digest in recorded.items():
        if rel not in actual:
            problems.append(f"文件缺失：{rel}")
        elif actual[rel] != digest:
            problems.append(f"内容被改动：{rel}")
    for rel in actual:
        if rel not in recorded:
            problems.append(f"封存后新增：{rel}")

    if seal.exists():
        try:
            meta = json.loads(seal.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append(f"{seal.name} 不是合法 JSON，封存记录不可信")
        else:
            recorded_hash = meta.get("sums_sha256")
            if not recorded_hash:
                problems.append(f"{seal.name} 缺少 sums_sha256，封存记录不完整")
            elif recorded_hash != sha256_of(sums):
                problems.append("SHA256SUMS.txt 本身被改过（封存记录不可信）")
    else:
        problems.append(f"缺少封存记录：{seal.name}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="lock 集封存 / 校验")
    ap.add_argument("action", choices=["seal", "verify"])
    args = ap.parse_args(argv)

    if args.action == "seal":
        meta = seal()
        print(f"已封存 {meta['image_count']} 张图 + {meta['label_count']} 份标注"
              f"（共 {meta['file_count']} 个文件）")
        print(f"  {SUMS_NAME} / {SEAL_NAME}")
        return 0

    problems = verify()
    if problems:
        print(f"lock 集已被改动，{len(problems)} 处：")
        for p in problems:
            print("  " + p)
        return 1
    print(f"lock 集完好：{len(collect(LOCK))} 个文件，与封存记录一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
