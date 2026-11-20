"""Check or refresh the deployment snapshot under ``app/vendor/photocheck``."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "photocheck"
TARGET = ROOT / "app" / "vendor" / "photocheck"
IGNORED_NAMES = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_NAMES for part in path.parts)
        and path.suffix not in IGNORED_SUFFIXES
    }


def differences() -> list[str]:
    source = _files(SOURCE)
    target = _files(TARGET)
    names = sorted(set(source) | set(target))
    return [
        name
        for name in names
        if name not in source
        or name not in target
        or not filecmp.cmp(source[name], target[name], shallow=False)
    ]


def sync() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(
        SOURCE,
        TARGET,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "*.pyo"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args()

    changed = differences()
    if args.check:
        if changed:
            print("app/vendor/photocheck differs from photocheck:")
            for name in changed[:40]:
                print(f"  {name}")
            if len(changed) > 40:
                print(f"  ... {len(changed) - 40} more")
            return 1
        print("app/vendor/photocheck is in sync")
        return 0

    sync()
    remaining = differences()
    if remaining:
        print(f"vendor sync failed: {len(remaining)} differences remain", file=sys.stderr)
        return 1
    print("app/vendor/photocheck synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
