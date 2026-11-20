"""Package A's complete delivery tree into one zip.

Why this script exists: hand-made zips repeated three mistakes - missing files,
`__pycache__` folders inside, and non-ASCII entry names that unpack as mojibake on
the other side. All three happened during the first handoff.

Usage:
    python scripts/build_delivery.py                 # -> dist/A_delivery_v2_<date>.zip
    python scripts/build_delivery.py --out D:/x.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Must ship (relative to the project root)
INCLUDE_DIRS = ("core", "badcases", "reports", "scripts", "tests", "samples")
INCLUDE_FILES = (
    "README.md",
    "交付说明_v2.md",
    "requirements.txt",
    "VERSION.json",
    "express_001.png",
    "express_001.json",
)

# Never ship
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".venv", "_archive", ".workbuddy", ".git"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# Review artifacts are shipped as their own zip (A_第三层判读素材_*.zip) because a
# reviewer needs to open them without the source tree. Including them here too
# doubled the delivery from 7.1 MB to 11.2 MB with files nobody diffs.
EXCLUDE_PREFIXES = (
    "reports/layer3_review",
    "reports/proto_na",
    "reports/proto_inline_bbox",
    "reports/t_ocr_scale",
)


def _collect() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"missing required file: {name}")
        files.append(path)

    for dirname in INCLUDE_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            raise SystemExit(f"missing required directory: {dirname}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(ROOT).parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(rel == pre or rel.startswith(pre + "/") for pre in EXCLUDE_PREFIXES):
                continue
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def _source_hash() -> str:
    """SHA-256 over the shipping code (core + scripts + tests), path-ordered.

    Deliberately NOT a hash of the zip. The zip contains README.md, and README
    has to state the version - so a zip hash written into README would change
    the zip that produced it, forever chasing its own tail. Hashing only the
    `.py` files breaks the loop: documenting the version cannot alter it.
    """
    code_roots = ("core", "scripts", "tests")
    parts: list[bytes] = []
    for dirname in code_roots:
        for path in sorted((ROOT / dirname).rglob("*.py"), key=lambda p: p.relative_to(ROOT).as_posix()):
            if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(ROOT).parts):
                continue
            rel = path.relative_to(ROOT).as_posix().encode("utf-8")
            parts.append(rel + b"\0" + path.read_bytes() + b"\0")
    return hashlib.sha256(b"".join(parts)).hexdigest().upper()


def _write_version_file(source_hash: str, build_time: str) -> Path:
    payload = {
        "artifact": "A delivery (PhotoCheck detection engine)",
        "built_at": build_time,
        "source_hash_sha256": source_hash,
        "source_hash_scope": "core/**, scripts/**, tests/** (*.py), path-ordered",
        "source_hash_algorithm": "sha256 over concat(relpath + NUL + bytes + NUL)",
        "python": sys.version.split()[0],
        "note": (
            "Verify with: python scripts/build_delivery.py --check-hash. "
            "The zip's own SHA-256 is published next to it as <zip>.sha256, not "
            "inside the zip, because writing it inside would make the archive "
            "depend on its own digest."
        ),
    }
    path = ROOT / "VERSION.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _update_readme_version(source_hash: str, build_time: str) -> None:
    """Rewrite the marked block in README so the version travels with the code."""
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = (
        "<!-- build:start -->\n"
        f"- 源码树哈希（`core/` `scripts/` `tests/` 全部 `.py`）：`{source_hash}`\n"
        f"- 构建时间：{build_time}\n"
        "- 交付包自身 SHA-256：见同目录 `<包名>.sha256`（**包外**，写进包内会让哈希自指）\n"
        "- 校验方式：`python scripts/build_delivery.py --check-hash`（比对源码树哈希）\n"
        "<!-- build:end -->"
    )
    if "<!-- build:start -->" in text:
        text = re.sub(r"<!-- build:start -->.*?<!-- build:end -->", block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## 版本与校验\n\n" + block + "\n"
    readme.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package A's delivery tree")
    parser.add_argument("--out", help="output zip path")
    parser.add_argument("--check-hash", action="store_true",
                        help="compare the current source tree with VERSION.json and exit")
    args = parser.parse_args(argv)

    source_hash = _source_hash()

    if args.check_hash:
        version_file = ROOT / "VERSION.json"
        if not version_file.is_file():
            print("[fail] VERSION.json is missing - the package was never built here")
            return 2
        recorded = json.loads(version_file.read_text(encoding="utf-8"))["source_hash_sha256"]
        same = recorded == source_hash
        print(f"recorded: {recorded}")
        print(f"current : {source_hash}")
        print("MATCH" if same else "MISMATCH - code changed since the package was built")
        return 0 if same else 1

    build_time = datetime.now().isoformat(timespec="seconds")
    _write_version_file(source_hash, build_time)
    _update_readme_version(source_hash, build_time)

    # The archive name stays Chinese because it is the artifact name the receiving
    # side already refers to; zipfile sets the UTF-8 entry flag (0x800) by itself,
    # so the Chinese file names inside unpack correctly.
    out = Path(args.out) if args.out else ROOT / "dist" / f"A_交付_v2_{date.today().isoformat()}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    files = _collect()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arc = path.relative_to(ROOT).as_posix()
            zf.write(path, arc)

    digest = hashlib.sha256(out.read_bytes()).hexdigest().upper()
    sidecar = out.with_name(out.name + ".sha256")
    sidecar.write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(f"packed {len(files)} file(s)")
    print(f"  output: {out}")
    print(f"  size:   {out.stat().st_size / 1024:.1f} KB")
    print(f"  SHA-256: {digest}")
    print(f"  written: {sidecar.name} (outside the archive, by design)")
    print(f"  source hash: {source_hash}")
    for path in files:
        print(f"    {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
