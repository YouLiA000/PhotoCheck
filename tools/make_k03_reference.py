#!/usr/bin/env python3
"""Create the frozen K-03 OCR reference set (k03_ref_v1)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "dataset" / "k03_ref_v1"
SEED = 20261115

SPECS = (
    ("R1", 900, 620, 4, 76, "sparse"),
    ("R2", 900, 620, 16, 864, "dense"),
    ("R3", 1120, 1560, 5, 95, "sparse"),
    ("R4", 1120, 1560, 20, 1080, "dense"),
    ("R5", 1600, 1200, 15, 810, "medium"),
)

ALPHABET = "privacy check reference text 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ "


def _line_lengths(total: int, count: int) -> list[int]:
    base, extra = divmod(total, count)
    return [base + (1 if index < extra else 0) for index in range(count)]


def _make_lines(total: int, count: int) -> list[str]:
    stream = (ALPHABET * (total // len(ALPHABET) + 2))[:total]
    lines: list[str] = []
    offset = 0
    for length in _line_lengths(total, count):
        lines.append(stream[offset : offset + length])
        offset += length
    return lines


def _font_for(lines: list[str], width: int, height: int, count: int) -> ImageFont.FreeTypeFont:
    candidates = (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
    )
    font_path = next((path for path in candidates if path.is_file()), None)
    margin = max(24, width // 30)
    max_width = width - margin * 2
    max_height = height - margin * 2
    for size in range(max(12, height // max(count, 1)), 10, -1):
        font = (
            ImageFont.truetype(str(font_path), size=size)
            if font_path
            else ImageFont.load_default()
        )
        widths = [font.getbbox(line)[2] - font.getbbox(line)[0] for line in lines]
        line_height = max(font.getbbox("Ag")[3] - font.getbbox("Ag")[1], size)
        if max(widths, default=0) <= max_width and line_height * count <= max_height:
            return font
    return ImageFont.load_default()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(root: Path = DEFAULT_ROOT, *, force: bool = False) -> dict:
    images = root / "images"
    images.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    if manifest_path.exists() and not force:
        raise FileExistsError(f"reference set already frozen: {manifest_path}")

    items = []
    for ident, width, height, line_count, chars, shape in SPECS:
        lines = _make_lines(chars, line_count)
        if sum(len(line) for line in lines) < chars:
            lines[-1] += "x" * (chars - sum(len(line) for line in lines))
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = _font_for(lines, width, height, line_count)
        margin = max(24, width // 30)
        line_height = max(1, (height - margin * 2) // line_count)
        for index, line in enumerate(lines):
            draw.text((margin, margin + index * line_height), line, fill="black", font=font)
        path = images / f"{ident}.png"
        image.save(path, format="PNG", optimize=False)
        items.append(
            {
                "id": ident,
                "file": path.relative_to(root).as_posix(),
                "width": width,
                "height": height,
                "lines": line_count,
                "drawn_chars": chars,
                "shape": shape,
                "sha256": _sha256(path),
            }
        )

    manifest = {
        "version": "k03_ref_v1",
        "seed": SEED,
        "generator": "python tools/make_k03_reference.py",
        "font": "C:/Windows/Fonts/arial.ttf",
        "character_count_definition": "drawn non-newline text characters before OCR",
        "items": items,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate frozen K-03 reference images")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    manifest = generate(Path(args.root), force=args.force)
    print(f"created {len(manifest['items'])} images at {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
