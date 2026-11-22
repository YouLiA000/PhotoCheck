"""ExifTool invocation and safe metadata classification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_METADATA_CACHE_LIMIT = 16
_METADATA_CACHE: OrderedDict[tuple[str, int, int, str], dict[str, Any]] = OrderedDict()
_METADATA_CACHE_LOCK = threading.Lock()

_ALLOWED_PNG_TECHNICAL_TAGS = {
    "BitDepth",
    "ColorType",
    "Compression",
    "Filter",
    "ImageHeight",
    "ImageWidth",
    "Interlace",
    "PixelsPerUnitX",
    "PixelsPerUnitY",
    "PixelUnits",
    "ProfileName",
    "SRGBRendering",
}
_ALLOWED_COMPOSITE_TAGS = {"ImageSize", "Megapixels"}


class MetadataScanError(ValueError):
    """Raised when ExifTool cannot return a trustworthy metadata record."""


def find_bundled_exiftool(root: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the ExifTool shipped under ``tools/`` or ``runtime/``.

    This is the single project-wide answer to "where is the bundled copy?".
    Application code, offline scripts, and environment checks all use it so a
    development checkout and a portable package resolve the prerequisite the
    same way.
    """

    bases = [Path(root)] if root is not None else []
    bases.append(PROJECT_ROOT)
    seen: set[Path] = set()
    for base in bases:
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)
        for candidate in (
            base / "runtime" / "exiftool" / "exiftool.exe",
            base / "tools" / "exiftool" / "exiftool.exe",
        ):
            if candidate.is_file():
                return candidate.resolve()
    return None


def resolve_exiftool(explicit_path: str | os.PathLike[str] | None = None) -> Path:
    if explicit_path is not None:
        candidate = Path(explicit_path)
        if candidate.is_file():
            return candidate
        raise MetadataScanError(f"ExifTool not found at explicit path: {candidate}")

    configured = os.environ.get("PHOTOCHECK_EXIFTOOL")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise MetadataScanError(
            f"PHOTOCHECK_EXIFTOOL does not point to a file: {candidate}"
        )

    runtime_root = Path(sys.executable).resolve().parent
    bundled = find_bundled_exiftool()
    discovered = shutil.which("exiftool")
    candidates = [
        runtime_root / "exiftool" / "exiftool.exe",
        bundled,
        Path(discovered) if discovered else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise MetadataScanError(
        "ExifTool not found; set PHOTOCHECK_EXIFTOOL or place it at "
        "tools\\exiftool\\exiftool.exe (development) or "
        "runtime\\exiftool\\exiftool.exe (portable)"
    )


def read_metadata(
    path: str | os.PathLike[str],
    *,
    exiftool_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read content metadata, excluding SourceFile and filesystem attributes."""

    exiftool = resolve_exiftool(exiftool_path).resolve()
    source_path = Path(path).resolve()
    try:
        stat = source_path.stat()
    except OSError:
        stat = None
    cache_key = (
        str(source_path),
        stat.st_size if stat else -1,
        stat.st_mtime_ns if stat else -1,
        str(exiftool),
    )
    if stat is not None:
        with _METADATA_CACHE_LOCK:
            cached = _METADATA_CACHE.get(cache_key)
            if cached is not None:
                _METADATA_CACHE.move_to_end(cache_key)
                return deepcopy(cached)
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
    command = [
        str(exiftool),
        "-json",
        "-G1",
        "-a",
        "-s",
        "-charset",
        "filename=UTF8",
        "-charset",
        "EXIF=UTF8",
        "-charset",
        "IPTC=UTF8",
        "-@",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
        input=f"{source_path}\n".encode("utf-8"),
            timeout=20,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetadataScanError("ExifTool metadata scan timed out") from exc
    except OSError as exc:
        raise MetadataScanError("ExifTool could not be started") from exc

    if completed.returncode != 0:
        raise MetadataScanError(
            f"ExifTool metadata scan failed with code {completed.returncode}"
        )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    if len(stdout) > 2_000_000:
        raise MetadataScanError("ExifTool metadata output exceeds the safety limit")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MetadataScanError("ExifTool returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise MetadataScanError("ExifTool returned an unexpected metadata structure")

    metadata = payload[0]
    metadata.pop("SourceFile", None)
    for key in list(metadata):
        if key.startswith(("System:", "File:", "ExifTool:")):
            metadata.pop(key)
    if stat is not None:
        with _METADATA_CACHE_LOCK:
            _METADATA_CACHE[cache_key] = deepcopy(metadata)
            _METADATA_CACHE.move_to_end(cache_key)
            while len(_METADATA_CACHE) > _METADATA_CACHE_LIMIT:
                _METADATA_CACHE.popitem(last=False)
    return metadata


def clear_metadata_cache() -> None:
    """Clear the process-local metadata cache (tests and explicit privacy purge)."""

    with _METADATA_CACHE_LOCK:
        _METADATA_CACHE.clear()


def exiftool_version(
    exiftool_path: str | os.PathLike[str] | None = None,
) -> str:
    """Return the executable version or raise a clear prerequisite error."""

    exiftool = resolve_exiftool(exiftool_path)
    try:
        completed = subprocess.run(
            [str(exiftool), "-ver"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MetadataScanError("ExifTool version check failed") from exc
    version = completed.stdout.decode("ascii", errors="replace").strip()
    if completed.returncode != 0 or not version:
        raise MetadataScanError("ExifTool version check returned no version")
    return version


def metadata_residual_tags(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """Return content metadata that is outside the sanitized technical allowlist."""

    residuals: list[str] = []
    for key in metadata:
        group, separator, name = key.partition(":")
        if not separator:
            residuals.append(key)
            continue
        if groups_are_system_metadata(group):
            continue
        if group == "JFIF" or group == "ICC_Profile" or group.startswith("ICC-"):
            continue
        if group == "PNG" and name in _ALLOWED_PNG_TECHNICAL_TAGS:
            continue
        if group == "Composite" and name in _ALLOWED_COMPOSITE_TAGS:
            continue
        residuals.append(key)
    return tuple(sorted(residuals))


def groups_are_system_metadata(group: str) -> bool:
    return group in {"System", "File", "ExifTool"}
