#!/usr/bin/env python3
"""打**全量**交付包：给项目负责人看整体。

三个打包脚本的分工，别混：

| 脚本 | 给谁 | 特点 |
|---|---|---|
| `make_handoff.py --to A/B` | 队友干活用 | 按责任人切分，各带所需部分 |
| `make_delivery.py` | 评审 / 存档 | `git archive`，**只含跟踪文件** |
| **`make_full_export.py`（本脚本）** | **项目负责人 / 归档** | 全量源码 + 文档 + 数据 + 工具 |

为什么不复用 `make_delivery.py`：它走 `git archive`，而项目里有两类**必须交付但被 gitignore**
的资产——`tools/exiftool/`（35 MB，团队统一版本）和 `dataset/real/`（实拍素材，可能含隐私，
按纪律不入库）。只给 git 内容，负责人拿到的是跑不起来的半套东西。

## 纳入范围

1. `git ls-files` 的全部文件，**排除 `.workbuddy/`**（那是助手的工作记忆，不是项目资产）
2. `tools/exiftool/` —— ExifTool 13.61 免安装版，规格卡要求团队统一这个版本
3. `dataset/real/` —— 14 张实拍素材（**全部虚构**）+ `manifest.csv`
4. `results/` —— 评测产物（CSV / JSON / MD），用于追溯"跑过什么"

## 排除范围（及原因）

| 路径 | 体积 | 为什么排除 |
|---|---:|---|
| `.venv/` | 1.2 GB | 开发环境，对方用 `pip install -r requirements.txt` 重建 |
| `dist/` | 760 MB | 构建产物。成品应用 `PhotoCheck_App_win64.zip` 单独发，不嵌套 |
| `work/` | 9 MB | 运行时产物，**含用户上传的真实照片**，绝不能进交付包 |
| `.git/` | 18 MB | 版本库。改给一份 `00_交付导读.md` 里的 commit 摘要 |
| `.workbuddy/` | — | 助手工作记忆 |
| `__pycache__/` `*.pyc` | — | 字节码缓存 |

## 命名

- 包：`PhotoCheck_full_<YYYYMMDD-HHMMSS>.zip`（纯 ASCII，避开解压工具的编码问题）
- 包内顶层：`PhotoCheck/`（同样是 ASCII）

用法：
    python tools/make_full_export.py
    python tools/make_full_export.py --out dist/xxx.zip
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
TOP = "PhotoCheck"

# 追加目录：gitignore 里、但属于项目资产
EXTRA_DIRS = ("tools/exiftool", "dataset/real", "results")

# 排除规则（相对路径的前缀或片段）
EXCLUDE_PREFIX = (
    ".venv/", ".git/", "dist/", "work/", ".workbuddy/",
    "__pycache__/", ".pytest_cache/", ".mypy_cache/",
)
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIX = (".pyc", ".pyo")


def tracked_files() -> list[Path]:
    """git 跟踪的文件。

    `-z` + `core.quotepath=false` 两个都要：默认情况下含中文的路径会被转义成
    `"docs/\\345\\217\\221..."`，`Path()` 拿它当字面路径就找不到文件，于是被**静默丢弃**。
    2026-11-07 踩过一次——整个 `docs/` 从包里消失，而脚本不报错。
    """
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout
    return [ROOT / p for p in out.split("\0") if p]


def wanted(rel: str) -> bool:
    if any(rel.startswith(p) for p in EXCLUDE_PREFIX):
        return False
    parts = rel.split("/")
    if any(p in EXCLUDE_PARTS for p in parts):
        return False
    return not rel.endswith(EXCLUDE_SUFFIX)


def collect() -> list[tuple[str, Path]]:
    """返回 [(包内路径, 磁盘路径)]，已去重排序。"""
    seen: dict[str, Path] = {}

    for p in tracked_files():
        rel = p.relative_to(ROOT).as_posix()
        if wanted(rel) and p.is_file():
            seen[rel] = p

    for d in EXTRA_DIRS:
        base = ROOT / d
        if not base.is_dir():
            raise SystemExit(f"追加目录不存在：{d}")
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if wanted(rel):
                seen[rel] = p

    return sorted(seen.items())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def build_readme(files: list[tuple[str, Path]], total: int, commit: str) -> str:
    tops: dict[str, int] = {}
    for rel, _ in files:
        head = rel.split("/")[0] if "/" in rel else "(根目录)"
        tops[head] = tops.get(head, 0) + 1

    rows = "\n".join(
        f"| `{k}/` | {v} |" for k, v in sorted(tops.items(), key=lambda x: -x[1]) if k != "(根目录)"
    )
    root_files = [r for r, _ in files if "/" not in r]

    return f"""# PhotoCheck · 全量交付包

生成时间：{datetime.now():%Y-%m-%d %H:%M}　·　对应提交：`{commit}`
包内文件：**{len(files)} 个**　·　解压后 **{human(total)}**

## 这是什么

**PhotoCheck · 照片隐私体检** —— 输入一张本地图片，输出一张不覆盖原图的脱敏副本，
并**逐区域回读复检**证明它真的被擦掉了。全本地 CPU 运行，不训练模型、不联网、不调任何 API。

检测对象：手机号（含 `+86` / `0086-` 多种写法）、身份证号（末位 X、校验位三态）、
二维码（Wi-Fi / 名片 / 收款码 / 链接）、人脸、拍摄信息（GPS / 时间 / 机型 / 软件 / 缩略图）。

## 怎么跑起来

```bash
python -m venv .venv
.venv\\Scripts\\activate            # Windows
pip install -r requirements.txt

python tools/check_env.py          # 环境自检，缺什么直接说
python tools/smoke.py              # 一条命令跑完全部验收（约 1 分钟）
python ui/app.py                   # 起界面，http://127.0.0.1:7860
```

**ExifTool 已随包**（`tools/exiftool/`，13.61 免安装），不用另外装。

## 目录结构

| 目录 | 文件数 |
|---|---:|
{rows}

根目录另有：{", ".join(f"`{r}`" for r in root_files)}

关键几处：

| 路径 | 是什么 |
|---|---|
| `contract/` | 契约 v4：JSON Schema + 校验器 + 风险规则 + 掩码（全项目单点实现） |
| `dataset/dev/` | 43 张合成样本（38 有效 + 5 张故意做坏）+ 契约真值 |
| `dataset/lock/` | 23 张锁定集，已字节级封存（`seal_lock.py verify` 守护） |
| `dataset/real/` | **14 张实拍素材，全部虚构**（9 张屏幕翻拍 + 5 张手写），附清单 |
| `eval/` | 评测器：匹配、指标、报告 |
| `ui/` | 界面（Gradio 三页：检测 / 处置 / 结论） |
| `audit/` | 进度审计器：40 项检查对照规格卡 |
| `app/` | 集成三人的可独立运行应用（自带 Web 界面 + 便携打包脚本） |
| `docs/` | 文档，从 `docs/HANDOFF.md`（当前状态）和 `docs/INDEX.md`（索引）进 |
| `results/` | 历次评测产物，可追溯 |
| `tools/exiftool/` | ExifTool 13.61，团队统一版本，**勿换** |

## 包里**没有**什么（有意排除）

| 路径 | 原因 |
|---|---|
| `.venv/` | 开发环境（1.2 GB），用 `requirements.txt` 重建 |
| `dist/` | 构建产物。成品应用 `PhotoCheck_App_win64.zip`（194 MB，解压双击即用）**单独提供** |
| `work/` | 运行时产物，**可能含真实上传照片**，按纪律绝不进交付包 |
| `.git/` | 版本库。需要历史请从仓库取，或看 `docs/HANDOFF.md` 的提交记录 |

## 解压注意

包内有 40+ 个中文文件名的文档。**`zipfile` 写入了 UTF-8 标志位，但部分解压工具不认**
（Git Bash 自带的 `unzip`、Windows 资源管理器都会解成乱码）。

推荐：用 7-Zip 解压，或用 Python：

```bash
python -c "import zipfile; zipfile.ZipFile('PhotoCheck_full_xxx.zip').extractall('.')"
```

如果已经解压乱码，重新用上面任一方式再解一次即可，**文件内容没有损坏**。

## 完整性校验

```bash
# 逐个文件核对（包内 MANIFEST.txt 里有 路径 + 字节数 + SHA-256）
python - <<'PY'
import hashlib, pathlib
bad = n = 0
for line in pathlib.Path('MANIFEST.txt').read_text(encoding='utf-8').splitlines():
    if line.startswith('#') or not line.strip():
        continue
    digest, size, rel = line.split('  ', 2)
    p = pathlib.Path(rel)
    n += 1
    if not p.is_file() or p.stat().st_size != int(size) or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
        bad += 1
print(f'核验 {{n}} 个文件，不匹配 {{bad}}')
PY
```
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="打全量交付包（给项目负责人）")
    ap.add_argument("--out", help="输出路径（默认 dist/PhotoCheck_full_<时间戳>.zip）")
    args = ap.parse_args()

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()

    files = collect()
    total = sum(p.stat().st_size for _, p in files)
    print(f"收集 {len(files)} 个文件，{human(total)}　（提交 {commit}）")

    DIST.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else DIST / f"PhotoCheck_full_{stamp}.zip"

    manifest_lines = ["# 路径 + 字节数 + SHA-256（相对包顶层 PhotoCheck/）",
                      f"# 生成于 {datetime.now():%Y-%m-%d %H:%M:%S}　提交 {commit}",
                      f"# 共 {len(files)} 个文件", ""]
    t0 = time.time()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, path in files:
            arc = f"{TOP}/{rel}"
            info = zipfile.ZipInfo(arc, date_time=time.localtime(path.stat().st_mtime)[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            zf.writestr(info, data)
            manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {len(data)}  {rel}")

        readme = build_readme(files, total, commit)
        for name, text in (
            (f"{TOP}/00_交付导读.md", readme.encode("utf-8")),
            (f"{TOP}/MANIFEST.txt", ("\n".join(manifest_lines) + "\n").encode("utf-8")),
        ):
            info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, text)

    digest = sha256_of(out)
    (out.parent / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"完成 {out.name}　{size_mb:.1f} MB　用时 {time.time() - t0:.0f}s")
    print(f"sha256 {digest}")
    print(f"路径 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
