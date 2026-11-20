"""Static and runtime checks for PhotoCheck's offline operation claim."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BLOCKED_IMPORTS = {
    "aiohttp",
    "http.client",
    "httpx",
    "requests",
    "socket",
    "urllib.request",
}
BLOCKED_CALLS = {
    "create_connection",
    "connect",
    "connect_ex",
    "getaddrinfo",
    "request",
    "socket",
    "urlopen",
}
IGNORED_FILES = {
    "photocheck/network_guard.py",
}
SOURCE_DIRS = ("app", "audit", "contract", "eval", "photocheck", "scripts", "tools", "ui")
# The local HTTP UI must create a listening socket. It is the only production
# source allowed to do so; outbound connect/getaddrinfo calls remain forbidden.
LOCAL_LISTENER_ALLOWLIST = {"app/server.py", "tools/build_app.py"}
LOOPBACK_HTTP_ALLOWLIST = {"tools/build_app.py"}


def _allow_blocked_import(module: str, relative: str) -> bool:
    if module == "socket" and relative in LOCAL_LISTENER_ALLOWLIST:
        return True
    return module == "urllib.request" and relative in LOOPBACK_HTTP_ALLOWLIST


def _is_loopback_url(node: ast.AST) -> bool:
    try:
        text = ast.unparse(node).lower()
    except (AttributeError, ValueError):
        return False
    return (
        "http://127.0.0.1" in text
        or "http://localhost" in text
        or "https://127.0.0.1" in text
        or "https://localhost" in text
    )


def scan_source(root: Path) -> dict[str, list[str]]:
    imports: list[str] = []
    calls: list[str] = []
    paths: list[Path] = []
    for directory in SOURCE_DIRS:
        base = root / directory
        if base.is_dir():
            paths.extend(base.rglob("*.py"))
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if (
            relative in IGNORED_FILES
            or "/tests/" in f"/{relative}"
            or relative.startswith("app/vendor/")
        ):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            imports.append(f"{relative}: unreadable source: {type(exc).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                for module in modules:
                    blocked = any(
                        module == name or module.startswith(name + ".")
                        for name in BLOCKED_IMPORTS
                    )
                    if blocked and not _allow_blocked_import(module, relative):
                        imports.append(f"{relative}:{node.lineno}: import {module}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                blocked = any(
                    module == name or module.startswith(name + ".")
                    for name in BLOCKED_IMPORTS
                )
                if blocked and not _allow_blocked_import(module, relative):
                    imports.append(f"{relative}:{node.lineno}: from {module} import ...")
            elif isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else None
                )
                allowed_socket = name == "socket" and relative in LOCAL_LISTENER_ALLOWLIST
                allowed_loopback = (
                    name == "urlopen"
                    and relative in LOOPBACK_HTTP_ALLOWLIST
                    and bool(node.args)
                    and _is_loopback_url(node.args[0])
                )
                if name in BLOCKED_CALLS and not (allowed_socket or allowed_loopback):
                    calls.append(f"{relative}:{node.lineno}: call {name}()")
    return {"imports": imports, "calls": calls}


def run_offline_smoke() -> dict[str, Any]:
    script = PROJECT_ROOT / "photocheck" / "scripts" / "run_end_to_end.py"
    sample = PROJECT_ROOT / "photocheck" / "detectors" / "a_engine" / "express_001.png"
    with tempfile.TemporaryDirectory() as temp_dir:
        report = Path(temp_dir) / "report.json"
        environment = os.environ.copy()
        environment["PHOTOCHECK_OFFLINE_GUARD"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                str(sample),
                "--output-dir",
                str(Path(temp_dir) / "output"),
                "--report",
                str(report),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            return {
                "ok": False,
                "returncode": completed.returncode,
                "stderr_tail": completed.stderr[-2000:],
            }
        payload = json.loads(report.read_text(encoding="utf-8"))
        return {
            "ok": payload.get("verification", {}).get("disposal_state") == "passed",
            "disposal_state": payload.get("verification", {}).get("disposal_state"),
            "detection_count": payload.get("detection_count"),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-smoke", action="store_true")
    args = parser.parse_args()

    static = scan_source(PROJECT_ROOT)
    result: dict[str, Any] = {
        "static": static,
        "static_ok": not static["imports"] and not static["calls"],
    }
    if args.run_smoke:
        result["runtime"] = run_offline_smoke()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["static_ok"]:
        return 1
    if args.run_smoke and not result["runtime"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
