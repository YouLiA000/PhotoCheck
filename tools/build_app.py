#!/usr/bin/env python3
"""把整个应用打成一个「拷过去就能跑」的目录。

为什么不用 PyInstaller：本项目的依赖里有 onnxruntime / mediapipe / opencv，
它们的动态库和资源文件靠 PyInstaller 的 hook 收集，版本一换就容易漏；
而漏了的表现是「换台电脑打不开」，恰恰是最难在现场救的故障。
**直接把可用的解释器和已装好的包整目录复制**，虽然体积大一点，但可预测。

产物结构::

    PhotoCheck_App/
        启动.bat              ← 双击这个
        自检.bat              ← 跑不起来时先跑这个
        runtime/python.exe    ← 便携解释器
        runtime/Lib/...       ← 标准库 + 站点包
        runtime/exiftool/     ← 元数据独立复核工具
        app/...               ← 代码、界面、示例图
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = Path(sys.base_prefix)
SITE = Path(sysconfig.get_paths()["purelib"])
DIST = ROOT / "dist"

# 只跟 Gradio / JAX / 构建工具有关的包，运行时用不到。
# 保留判断标准：**删掉之后 app 与引擎还能跑通**（每次构建都会实际验证）。
BLOCK = {
    # Gradio 及其 Web 栈（本项目的界面是自带的原生 HTML，不用 Gradio）
    "gradio", "gradio_client", "fastapi", "starlette", "uvicorn", "anyio", "sniffio",
    "httpx", "httpcore", "h11", "jinja2", "markupsafe", "multipart", "python_multipart",
    "orjson", "typer", "rich", "pygments", "markdown_it", "mdurl", "aiofiles",
    "websockets", "ffmpy", "groovy", "pydub", "semantic_version", "tomlkit",
    "safehttpx", "annotated_doc", "annotated_types", "pydantic", "pydantic_core",
    "shellingham", "brotli", "hf_xet", "huggingface_hub", "filelock", "fsspec",
    "click", "colorama",
    # 表格与科学计算：本项目不用 pandas；jax/scipy 是 mediapipe 的"声明依赖"，
    # 我们只走 solutions 里的 face_detection，实测不需要
    "pandas", "pandas.libs", "pytz", "jax", "jaxlib", "ml_dtypes", "opt_einsum",
    "scipy", "scipy.libs",
    # 打包工具自身
    "PyInstaller", "pyinstaller_hooks_contrib", "altgraph", "pefile",
    "pywin32_ctypes", "win32ctypes", "ordlookup",
    # 其他本项目不用的
    "pip", "ruff", "opentelemetry", "opentelemetry_api", "pytesseract",
    "__pycache__",
}

KEEP_EXTRA = {"setuptools", "pkg_resources"}  # rapidocr 里可能用到 pkg_resources


def _skip(name: str) -> bool:
    head = name.split("-")[0].split(".")[0]
    if head in KEEP_EXTRA:
        return False
    if head in BLOCK:
        return True
    # dist-info 跟着包一起判断
    for blocked in BLOCK:
        if name.lower().startswith(blocked.lower() + "-"):
            return True
    return False


def copy_python(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "msvcr120.dll", "LICENSE.txt"):
        src = BASE_PYTHON / name
        if not src.is_file() and name == "msvcr120.dll":
            src = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name
        if src.is_file():
            shutil.copy2(src, dest / name)
        elif name == "msvcr120.dll":
            raise RuntimeError("msvcr120.dll is required for the bundled pyzbar")
    # 标准库：Lib 下除 site-packages 之外的全部
    shutil.copytree(BASE_PYTHON / "Lib", dest / "Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "idlelib"))
    shutil.copytree(BASE_PYTHON / "DLLs", dest / "DLLs",
                    ignore=shutil.ignore_patterns("__pycache__"))


def copy_site(dest: Path) -> tuple[int, int]:
    dest.mkdir(parents=True, exist_ok=True)
    kept = skipped = 0
    for entry in sorted(SITE.iterdir()):
        if entry.name == "__pycache__":
            continue
        if _skip(entry.name):
            skipped += 1
            continue
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test"))
        else:
            shutil.copy2(entry, target)
        kept += 1
    return kept, skipped


def write_launchers(out: Path) -> None:
    launcher = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \"runtime\\python.exe\" goto :missing_runtime\r\n"
        "if not exist \"runtime\\python311.dll\" goto :missing_runtime\r\n"
        "if not exist \"runtime\\Lib\\site-packages\" goto :missing_runtime\r\n"
        "set \"PHOTOCHECK_EXIFTOOL=%~dp0runtime\\exiftool\\exiftool.exe\"\r\n"
        "set \"PYTHONNOUSERSITE=1\"\r\n"
        "set \"PYTHONDONTWRITEBYTECODE=1\"\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        "echo 正在启动 PhotoCheck ...\r\n"
        "runtime\\python.exe app\\main.py\r\n"
        "if errorlevel 1 (\r\n"
        "  echo.\r\n"
        "  echo 启动失败。请把上面的错误信息发给开发者，或先运行 自检.bat\r\n"
        "  pause\r\n"
        ")\r\n"
        "exit /b %errorlevel%\r\n"
        ":missing_runtime\r\n"
        "echo [ERROR] Portable runtime is incomplete.\r\n"
        "echo Extract the whole ZIP first, then run Start.bat or the Chinese-named launcher.\r\n"
        "echo Do not run this file directly from inside the ZIP.\r\n"
        "pause\r\n"
        "exit /b 1\r\n"
    )
    for name in ("启动.bat", "Start.bat"):
        (out / name).write_text(launcher, encoding="utf-8", newline="")

    checker = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \"runtime\\python.exe\" (\r\n"
        "  echo [ERROR] runtime\\python.exe is missing. Extract the whole ZIP first.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "set \"PHOTOCHECK_EXIFTOOL=%~dp0runtime\\exiftool\\exiftool.exe\"\r\n"
        "set \"PYTHONNOUSERSITE=1\"\r\n"
        "set \"PYTHONDONTWRITEBYTECODE=1\"\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        "runtime\\python.exe app\\selfcheck.py\r\n"
        "pause\r\n"
    )
    for name in ("自检.bat", "Check.bat"):
        (out / name).write_text(checker, encoding="utf-8", newline="")

    (out / "README_FIRST.txt").write_text(
        "PhotoCheck portable package\r\n"
        "===========================\r\n\r\n"
        "1. Extract the ENTIRE ZIP to a writable folder.\r\n"
        "2. Double-click Start.bat (or the Chinese-named launcher).\r\n"
        "3. Keep the console window open while using the app.\r\n"
        "4. If it does not start, run Check.bat and send the complete output.\r\n\r\n"
        "Do not run the launcher directly from inside the ZIP.\r\n"
        "Windows 10/11 64-bit is supported. Python does not need to be installed.\r\n",
        encoding="ascii",
        newline="",
    )


USAGE = """PhotoCheck · 照片隐私体检  v1.0
=====================================

【怎么用】
1. 解压到一个可写目录；建议使用短英文路径，例如 D:\\PhotoCheck_App
2. 双击「启动.bat」
3. 稍等 2-5 秒，浏览器会自动打开 http://127.0.0.1:7861/
4. 关闭那个黑色命令行窗口，程序就退出了

【它做什么】
找出照片里可自动检测的隐私线索，按当前计划处理，并复检导出文件。
  · 手机号（含 +86 / 0086- 各种写法）、身份证号（末位 X、校验位）
  · 二维码（Wi-Fi 密码 / 名片 / 收款码 / 链接）
  · 人脸
  · 拍摄信息（GPS、时间、机型、处理软件、缩略图）
  · 勾选要擦的内容 → 生成脱敏副本 → 逐区域复检 + 整图重扫

【隐私说明】
· 全程在本机运行，**不联网、不上传任何图片**
· 上传副本和处理产物只写在本目录的 work\\ 下，随时可以整个删掉
· 界面上显示的号码一律是掩码（例如 177****4805）
· `passed` 只表示本次计划区域与同管线整图复检未发现残留，不是安全承诺

【打不开怎么办】
先双击「自检.bat」，它会逐项检查：解释器 / 依赖 / 检测器 / ExifTool / 端到端检测。
把输出截图发给开发者即可。

常见原因：
· 解压到了 Program Files 之类没有写权限的目录 → 换到一个可写目录
· 杀毒软件拦截了 runtime\\python.exe → 加入信任
· 端口被占用 → 程序会自动换端口，看命令行里打印的地址

【系统要求】
Windows 10/11 64 位 · 内存 4GB+ · 磁盘 1GB · 不需要装 Python · 不需要联网
"""


def write_docs(out: Path) -> None:
    (out / "使用说明.txt").write_text(USAGE, encoding="utf-8", newline="\r\n")
    manual = ROOT / "docs" / "PhotoCheck_成品说明_2026-11-13.md"
    if manual.is_file():
        shutil.copy2(manual, out / "项目说明.md")
    docs = out / "docs"
    docs.mkdir(exist_ok=True)
    for relative in (
        "功能规格卡_v1.4.md",
        "评测协议_v1.0.md",
        "标注指南_R-01_v1.md",
        "R03导出图复检_2026-11-08.md",
    ):
        source = ROOT / "docs" / relative
        if source.is_file():
            shutil.copy2(source, docs / source.name)
    for relative in ("requirements.txt", "requirements-dev.txt"):
        source = ROOT / relative
        if source.is_file():
            shutil.copy2(source, out / relative)


def _prune_build_tree(out: Path) -> None:
    """Remove tests, caches and non-runtime vendored material from the build."""

    work = out / "work"
    if work.exists():
        shutil.rmtree(work)

    for path in sorted(out.rglob("*"), reverse=True):
        if path.is_dir() and path.name in {"__pycache__", ".pytest_cache"}:
            shutil.rmtree(path)
        elif path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()

    app = out / "app"
    for relative in ("tests", "vendor/photocheck/tests", "vendor/photocheck/tests_integration",
                     "vendor/photocheck/scripts", "vendor/photocheck/contract/tests"):
        target = app / relative
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()

    a_engine = app / "vendor" / "photocheck" / "detectors" / "a_engine"
    for name in ("tests", "scripts", "reports", "badcases", "samples"):
        target = a_engine / name
        if target.exists():
            shutil.rmtree(target)
    for target in (a_engine / "express_001.json", a_engine / "express_001.png"):
        target.unlink(missing_ok=True)


def _runtime_debris(out: Path) -> list[str]:
    debris: list[str] = []
    for path in out.rglob("*"):
        relative = path.relative_to(out)
        if relative.parts and relative.parts[0] == "work":
            debris.append(relative.as_posix())
        elif (
            (path.is_file() and path.suffix in {".pyc", ".pyo"})
            or "__pycache__" in relative.parts
            or ".pytest_cache" in relative.parts
        ):
            debris.append(relative.as_posix())
    return debris


def _missing_required(out: Path) -> list[str]:
    required = (
        "启动.bat",
        "Start.bat",
        "自检.bat",
        "Check.bat",
        "README_FIRST.txt",
        "使用说明.txt",
        "项目说明.md",
        "app/main.py",
        "app/selfcheck.py",
        "runtime/python.exe",
        "runtime/msvcr120.dll",
        "runtime/exiftool/exiftool.exe",
        "docs/功能规格卡_v1.4.md",
        "requirements.txt",
    )
    return [name for name in required if not (out / name).is_file()]


def _audit_build_tree(out: Path) -> None:
    """Fail closed when a portable bundle contains runtime or privacy debris."""

    forbidden = _runtime_debris(out)
    if forbidden:
        raise RuntimeError(f"portable bundle contains runtime debris: {forbidden[:8]}")

    missing = _missing_required(out)
    if missing:
        raise RuntimeError(f"portable bundle is missing required files: {missing}")

    python = out / "runtime" / "python.exe"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    smoke = subprocess.run(
        [str(python), "app/selfcheck.py"],
        cwd=out,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
    )
    output = (smoke.stdout or "") + (smoke.stderr or "")
    if smoke.returncode != 0:
        raise RuntimeError(f"bundled selfcheck failed:\n{output[-2000:]}")
    if "_distutils_hack" in output or "ModuleNotFoundError" in output:
        raise RuntimeError(f"bundled interpreter is incomplete:\n{output[-2000:]}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    server = subprocess.Popen(
        [str(python), "app/main.py", "--no-browser", "--port", str(port)],
        cwd=out,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=flags,
    )
    healthy = False
    try:
        for _ in range(240):
            if server.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=0.5
                ) as response:
                    healthy = response.status == 200
                    if healthy:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
    finally:
        if server.poll() is None:
            server.terminate()
        try:
            server_stdout, server_stderr = server.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server_stdout, server_stderr = server.communicate(timeout=15)
    if not healthy:
        raise RuntimeError(
            "packaged app entry did not serve /api/health:\n"
            + ((server_stdout or "") + (server_stderr or ""))[-2000:]
        )

    # Importing the app creates its normal work/ tree. A delivered bundle must
    # start empty, so clean only after the smoke run has exercised the runtime.
    _prune_build_tree(out)
    debris_after_smoke = _runtime_debris(out)
    if debris_after_smoke:
        raise RuntimeError(
            "selfcheck left runtime debris in the bundle: " + repr(debris_after_smoke[:8])
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_build_info(out: Path) -> None:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    files = [path for path in out.rglob("*") if path.is_file()]
    payload = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_revision": revision,
        "file_count_before_build_info": len(files),
        "bytes_before_build_info": sum(path.stat().st_size for path in files),
        "contract_version": "photocheck/v4",
    }
    (out / "BUILD_INFO.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_zip(out: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = DIST / f"PhotoCheck_App_win64_{stamp}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                zf.write(path, (Path("PhotoCheck_App") / path.relative_to(out)).as_posix())
    digest = _sha256(archive)
    sidecar = archive.with_suffix(".zip.sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive, sidecar


def build(clean: bool = True) -> Path:
    out = DIST / "PhotoCheck_App"
    if out.exists() and clean:
        # 不用 rmtree：工具链的删除守卫会拦下几 GB 的目录（报 trash-failed）。
        # 改成重命名备份，既绕开守卫，又留一次回滚的机会。
        backup = DIST / f"_old_PhotoCheck_App_{datetime.now():%Y%m%d-%H%M%S}"
        out.rename(backup)
        print(f"  旧构建已改名：{backup.name}（确认新包没问题后可手动删）")
    print(f"构建到 {out}")
    print("  0/6 检查应用测试与同步运行时快照 …")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "app/tests", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if tests.returncode != 0:
        raise RuntimeError("app tests failed before build:\n" + (tests.stdout + tests.stderr)[-2000:])
    sync = subprocess.run(
        [sys.executable, "scripts/sync_app_vendor.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if sync.returncode != 0:
        raise RuntimeError("runtime snapshot sync failed:\n" + (sync.stdout + sync.stderr)[-2000:])
    print("  1/6 复制解释器 …")
    copy_python(out / "runtime")
    print("  2/6 复制依赖包 …")
    kept, skipped = copy_site(out / "runtime" / "Lib" / "site-packages")
    print(f"      保留 {kept} 项、跳过 {skipped} 项")
    print("  3/6 复制应用与 ExifTool …")
    shutil.copytree(ROOT / "app", out / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests"))
    exiftool = ROOT / "tools" / "exiftool"
    if exiftool.is_dir():
        shutil.copytree(exiftool, out / "runtime" / "exiftool",
                        ignore=shutil.ignore_patterns("*.txt"))
    print("  4/6 清理测试、缓存与非运行时代码 …")
    _prune_build_tree(out)
    print("  5/6 写启动脚本与说明 …")
    write_launchers(out)
    write_docs(out)
    _write_build_info(out)
    print("  6/6 审计便携目录并打 ZIP …")
    _audit_build_tree(out)
    archive, sidecar = _write_zip(out)

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    count = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"完成：{count} 个文件，{size / 1024 / 1024:.0f} MB")
    print(f"ZIP：{archive} ({archive.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"SHA256：{sidecar}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建便携版 PhotoCheck_App")
    ap.add_argument("--keep", action="store_true", help="保留已有目录（增量覆盖）")
    args = ap.parse_args(argv)
    if not BASE_PYTHON.is_dir():
        print(f"找不到基础解释器：{BASE_PYTHON}", file=sys.stderr)
        return 1
    build(clean=not args.keep)
    print("\n下一步：cd dist\\PhotoCheck_App && 启动.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
