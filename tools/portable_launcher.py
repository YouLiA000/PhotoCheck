#!/usr/bin/env python3
"""便携版启动器：双击 PhotoCheck.exe 即可运行。

为什么单独做启动器而不是把整个应用打成 onefile：

- 应用依赖 mediapipe / opencv / gradio，onefile 每次启动都要解压 500 MB+ 到临时目录，
  冷启动 20–60 秒，且杀软误报率高；
- 便携目录（runtime/ 里是完整解释器）启动是秒级，出问题还能直接看日志。

本启动器只做四件事：定位同目录的 runtime\\python.exe、检查 app\\main.py、
以该目录为工作目录启动、把退出码透传出去。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def base_dir() -> Path:
    """打包成 exe 后取 exe 所在目录；源码运行时取脚本上级目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def fail(message: str) -> int:
    print(f"[PhotoCheck] {message}", file=sys.stderr)
    try:
        import tkinter.messagebox as mb
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        mb.showerror("PhotoCheck failed to start", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - 没有 tk 也要保证有 stderr 输出
        pass
    return 2


def selftest(root: Path) -> int:
    """只检查目录结构与解释器可用性，不启动应用。"""
    python = root / "runtime" / "python.exe"
    entry = root / "app" / "main.py"
    vendor = root / "app" / "vendor" / "photocheck"
    exiftool = root / "runtime" / "exiftool" / "exiftool.exe"

    checks = [
        ("base dir", root.is_dir(), str(root)),
        ("interpreter", python.is_file(), str(python)),
        ("app entry", entry.is_file(), str(entry)),
        ("vendor snapshot", vendor.is_dir(), str(vendor)),
        ("exiftool", exiftool.is_file(), str(exiftool)),
    ]
    ok = True
    for name, passed, detail in checks:
        print(f"  {'OK  ' if passed else 'FAIL'}  {name:10s} {detail}")
        ok = ok and passed

    if python.is_file():
        probe = subprocess.run([str(python), "-c", "import sys;print(sys.version.split()[0])"],
                               capture_output=True, text=True)
        version = probe.stdout.strip() or probe.stderr.strip()
        print(f"  {'OK  ' if probe.returncode == 0 else 'FAIL'}  interpreter runs {version}")
        ok = ok and probe.returncode == 0

    print("\nSelf-test passed. Double-click PhotoCheck.exe to start." if ok else "\nSelf-test failed, see FAIL lines above.")
    return 0 if ok else 3


def main() -> int:
    root = base_dir()
    if "--selftest" in sys.argv[1:]:
        return selftest(root)

    python = root / "runtime" / "python.exe"
    entry = root / "app" / "main.py"

    if not python.is_file():
        return fail(
            f"Runtime not found: {python}\n\n"
            "Keep PhotoCheck.exe and the runtime\\ folder in the same directory, "
            "and make sure the whole folder was extracted."
        )
    if not entry.is_file():
        return fail(f"Application entry not found: {entry}\n\nCheck that the app\\ folder is complete.")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "app")
    env.setdefault("PYTHONUTF8", "1")

    print(f"[PhotoCheck] interpreter: {python}")
    print(f"[PhotoCheck] working dir: {root}")
    print("[PhotoCheck] starting local service, the browser will open shortly ...")
    print("[PhotoCheck] close this window to stop the program.")

    try:
        return subprocess.call([str(python), str(entry)], cwd=str(root), env=env)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        return fail(f"failed to start: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
