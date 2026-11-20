"""Temporary stand-in for B's K-01 (image loading and metadata parsing).

DELETE THIS FILE once B's `core/io/loader.py` lands. It only does the two things
A actually needs:
1. `ImageOps.exif_transpose` - fix orientation first, then fix the coordinate
   system. Without this every bbox on a portrait photo is silently rotated.
2. Emit an `ImageDescriptor` matching the contract (oriented width/height plus
   top_left_pixels).

It does not parse EXIF fields (that is B's job) and does not read thumbnails.

Filename handling is a real-world problem, not a theoretical one. The v4
Identifier rule allows `[A-Za-z0-9_.:-]` starting with an alphanumeric, but the
file names that actually arrive from a phone or WeChat are things like
`照片.png`, `IMG 1234.JPG`, `截图(1).png` or `_DSC0001.JPG`. Feeding the raw stem
into the contract raises ContractError and the whole analysis dies on nothing
worse than a Chinese file name - so the automatic path sanitises the stem, and
only an explicitly supplied `image_id` is passed through untouched (that one is
the caller's promise to keep, and silently rewriting it would hide their bug).
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

from ..contract import ImageDescriptor

__all__ = ["load_working_image", "describe", "sanitize_image_id"]

# Same character set the v4 Identifier rule allows, but without the "must start
# with an alphanumeric" part - the prefix is added separately below.
_ID_BODY = re.compile(r"[^A-Za-z0-9_.:\-]")
_ID_MAX_LEN = 128


def sanitize_image_id(raw: str) -> str:
    """Turn an arbitrary file stem into a legal v4 Identifier.

    Guarantees, in order:
    1. only `[A-Za-z0-9_.:-]` survive; everything else (CJK, spaces, brackets,
       emoji) becomes `_`. Two Chinese names that differ only by the characters
       that got replaced would collide, so;
    2. whenever anything was replaced, a short CRC32 suffix is appended to keep
       distinct files distinct - the id has to survive a round trip through a
       report, not just be legal;
    3. the first character is forced to an alphanumeric by prefixing `img`;
    4. the result is clipped to 128 characters *after* the suffix is attached,
       so truncation cannot cut the uniqueness off.
    """
    text = str(raw or "")
    cleaned = _ID_BODY.sub("_", text)
    replaced = cleaned != text
    needs_prefix = not cleaned or not (cleaned[0].isascii() and cleaned[0].isalnum())
    if needs_prefix:
        cleaned = f"img{cleaned}" if cleaned else "img"
    # Truncation also has to leave a fingerprint: two long names that differ only
    # past the 128th character would otherwise collapse into the same id.
    if replaced or needs_prefix or len(cleaned) > _ID_MAX_LEN:
        # `zlib.crc32` is stable across processes and platforms (unlike `hash`),
        # which matters because the id appears in saved reports.
        suffix = f"-{zlib.crc32(text.encode('utf-8')) & 0xFFFFFF:06x}"
    else:
        suffix = ""
    if len(cleaned) + len(suffix) > _ID_MAX_LEN:
        cleaned = cleaned[: _ID_MAX_LEN - len(suffix)]
    return f"{cleaned}{suffix}"


def _open_image(path: Path):
    from PIL import Image, ImageOps

    try:  # HEIC support is optional; skip it when the plugin is not installed
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass

    with Image.open(path) as raw:
        oriented = ImageOps.exif_transpose(raw)
        return oriented.convert("RGB")


def load_working_image(path: str | Path, image_id: str | None = None):
    """Return (PIL.Image, ImageDescriptor). Orientation already fixed via EXIF."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"image not found: {p}")
    image = _open_image(p)
    descriptor = describe(image, image_id or sanitize_image_id(p.stem))
    return image, descriptor


def describe(image, image_id: str) -> ImageDescriptor:
    width, height = image.size
    return ImageDescriptor(image_id=image_id, width=int(width), height=int(height))
