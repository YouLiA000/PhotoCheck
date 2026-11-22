#!/usr/bin/env python3
"""环境自检（K-15 / DoD「在别人的电脑上跑通过一次」的入口）。

    python tools/check_env.py

只读检查，不装包、不改配置。退出码 0 = 全部必需项就绪；1 = 有缺失。
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photocheck.core.metadata import MetadataScanError, resolve_exiftool

OK, BAD, WARN = "OK", "FAIL", "WARN"

# 团队统一固定的 ExifTool 版本。固定不是洁癖：K-02/K-09 的复核结论要能互相比对，
# 同一个 -a -G1 输出在不同版本间可能增删字段，版本不一致就说不清"差异是图变了还是工具变了"。
EXPECTED_EXIFTOOL = "13.61"

# (发行包名, 导入名, 规格卡 §0.3 要求, 是否必需)
PACKAGES = [
    ("gradio", "gradio", ">=5,<6", False),
    ("pillow", "PIL", "10.x", True),
    ("pillow-heif", "pillow_heif", ">=0.18", False),
    # Spec v1.4 section 0.3 lists opencv-python; the project ships opencv-contrib-python
    # instead (a superset). Both write into the same cv2 namespace, so installing both
    # makes the imported version depend on install order.
    ("opencv-contrib-python", "cv2", "4.10.x", True),
    ("piexif", "piexif", "1.1.3", True),
    ("rapidocr-onnxruntime", "rapidocr_onnxruntime", "1.4.x", True),
    ("pyzbar", "pyzbar.pyzbar", "0.1.9", True),
    ("mediapipe", "mediapipe", "0.10.x", False),
    ("numpy", "numpy", "1.26.x", True),
    ("pytesseract", "pytesseract", "0.3.x", False),
    ("pyinstaller", "PyInstaller", "6.x", False),
    ("jsonschema", "jsonschema", ">=4.21,<5", False),
    ("qrcode", "qrcode", ">=7.4", False),
]

rows: list[tuple[str, str, str]] = []
ok = True


def line(name: str, value: str, mark: str) -> None:
    rows.append((name, value, mark))
    print(f"  {name:22s} {value:34s} {mark}")


def _ver_tuple(text: str) -> tuple[int, ...]:
    """把 "5.9.0" / "1.4.x" / "0.10.14rc1" 解析成数字元组（遇到非数字即截断该段）。"""
    parts: list[int] = []
    for piece in str(text).strip().split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """按补零后逐位比较，返回 -1 / 0 / 1。"""
    n = max(len(a), len(b))
    left = a + (0,) * (n - len(a))
    right = b + (0,) * (n - len(b))
    return (left > right) - (left < right)


def parse_spec(spec: str) -> list[tuple[str, tuple[int, ...]]]:
    """解析区间记法：">=5,<6"、"10.x"、"1.1.3"、">=4.21,<5"。"""
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw in str(spec).split(","):
        part = raw.strip()
        if not part:
            continue
        if part.endswith(".x"):
            clauses.append(("~", _ver_tuple(part[:-2])))
        elif part.startswith(">="):
            clauses.append((">=", _ver_tuple(part[2:])))
        elif part.startswith("<"):
            clauses.append(("<", _ver_tuple(part[1:])))
        else:
            clauses.append(("==", _ver_tuple(part)))
    return clauses


def in_range(version: str, spec: str) -> tuple[bool | None, str]:
    """返回 (是否符合, 备注)；None 表示版本串无法解析，调用方应按 WARN 处理。

    2026-11-20 修正：`PACKAGES` 早就写了 `>=5,<6`、`10.x` 这些区间，但
    `check_packages` 只把它打印出来、从不比较——装了 gradio 6.x 照样报 OK，
    DoD「在别人电脑上跑通一次」的入口形同装饰。
    """
    tv = _ver_tuple(version)
    if not tv or all(v == 0 for v in tv):
        return None, "版本串无法解析"
    for op, target in parse_spec(spec):
        if op == "~":
            if _cmp(tv[: len(target)], target) != 0:
                return False, ""
        elif op == ">=":
            if _cmp(tv, target) < 0:
                return False, ""
        elif op == "<":
            if _cmp(tv, target) >= 0:
                return False, ""
        elif _cmp(tv[: len(target)], target) != 0:
            return False, ""
    lower = str(version).lower()
    note = "预发布版，仅提示" if any(t in lower for t in ("rc", "alpha", "beta", "dev", "pre")) else ""
    return True, note


def check_python() -> None:
    v = sys.version_info
    good = (v.major, v.minor) == (3, 11)
    line("Python", f"{v.major}.{v.minor}.{v.micro}", OK if good else BAD)
    if not good:
        print("     规格卡 §0.3 要求 Python 3.11（3.13 缺 wheel）。当前不是 3.11。")
        globals()["ok"] = False


def check_packages() -> None:
    for dist, module, want, required in PACKAGES:
        try:
            version = md.version(dist)
        except md.PackageNotFoundError:
            version = None
        if version is None:
            line(dist, "未安装", BAD if required else WARN)
            if required:
                globals()["ok"] = False
            continue
        try:
            importlib.import_module(module)
        except Exception as exc:  # 装了但导入不了，比没装更麻烦
            line(dist, f"{version} 导入失败：{type(exc).__name__}", BAD)
            globals()["ok"] = False
            continue
        verdict, note = in_range(version, want)
        if verdict is None:
            line(dist, f"{version}   ({want})", WARN)
            continue
        if not verdict:
            # 必需包越界才算失败；可选包越界只提示，别挡路
            line(dist, f"{version}   ({want} 不符)", BAD if required else WARN)
            if required:
                globals()["ok"] = False
            continue
        line(dist, f"{version}   ({want}){'' if not note else '  ' + note}", OK)


def check_qr_roundtrip() -> None:
    """自己生成一个码再解出来：验证 pyzbar 的 DLL 真的能加载。"""
    try:
        import qrcode
        from PIL import Image
        from pyzbar.pyzbar import decode

        img = qrcode.make("photocheck-env-check").convert("RGB")
        got = decode(img)
        payload = got[0].data.decode() if got else ""
        good = payload == "photocheck-env-check"
        line("pyzbar 解码链路", payload or "解不出", OK if good else BAD)
        if not good:
            globals()["ok"] = False
    except Exception as exc:
        line("pyzbar 解码链路", f"{type(exc).__name__}: {exc}", BAD)
        globals()["ok"] = False


def _clean_env() -> dict:
    """Git Bash 会把 LC_ALL 设成 Windows Perl 不认的值，调 exiftool 时会刷一堆 locale 警告。"""
    env = {k: v for k, v in os.environ.items() if k not in ("LC_ALL", "LANG")}
    return env


def check_binaries() -> None:
    # 装在默认目录但没加 PATH 的情况太常见了，这里替它兜一层
    tess = shutil.which("tesseract") or next(
        (c for c in (
            r"C://Program Files//Tesseract-OCR//tesseract.exe",
            r"D://Program Files//Tesseract-OCR//tesseract.exe",
            r"D://Tesseract-OCR//tesseract.exe",
        ) if Path(c).exists()),
        None,
    )
    if tess:
        try:
            out = subprocess.run(
                [tess, "--list-langs"], capture_output=True, text=True, timeout=20
            ).stdout
            has_cn = "chi_sim" in out
            line("Tesseract", "已安装" + ("" if has_cn else "（缺 chi_sim）"), OK if has_cn else BAD)
            if not has_cn:
                print("     中文语言包缺失：重装时勾选 Chinese Simplified。R-04 需要。")
        except Exception as exc:
            line("Tesseract", f"调用失败 {type(exc).__name__}", BAD)
    else:
        line("Tesseract", "未安装", WARN)
        print("     R-04 需要。装好后再跑一次本脚本。")

    try:
        exif = resolve_exiftool()
        ver = subprocess.run(
            [str(exif), "-ver"], capture_output=True, text=True, timeout=30,
            env=_clean_env(),
        )
        version = ver.stdout.strip()
        if ver.returncode != 0 or not version:
            raise MetadataScanError("version command failed")
        where = "项目内" if exif.parent.name == "exiftool" else "PATH/外部"
        if version == EXPECTED_EXIFTOOL:
            line("ExifTool", f"{version}（{where}）", OK)
        else:
            line("ExifTool", f"{version}（{where}，期望 {EXPECTED_EXIFTOOL}）", BAD)
            globals()["ok"] = False
            print(f"     团队统一 ExifTool {EXPECTED_EXIFTOOL}，当前 {version}。")
            print("     换法：下载 exiftool-<ver>_64.zip 解压覆盖 tools/exiftool/，")
            print("           把 exiftool(-k).exe 另存为 exiftool.exe，重跑本脚本。")
    except (MetadataScanError, OSError, subprocess.SubprocessError) as exc:
        line("ExifTool", f"不可用 {type(exc).__name__}", BAD)
        globals()["ok"] = False
        print("     ExifTool 是硬依赖。设置 PHOTOCHECK_EXIFTOOL，或解压 13.61 到 tools/exiftool/。")


def main() -> int:
    print("PhotoCheck 环境自检（规格卡 §0.3 / §0.4 DoD）\n")
    check_python()
    print()
    check_packages()
    print()
    check_qr_roundtrip()
    print()
    check_binaries()
    print()
    if ok:
        print("必需项全部就绪。")
    else:
        print("有必需项缺失，按上面的提示处理。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
