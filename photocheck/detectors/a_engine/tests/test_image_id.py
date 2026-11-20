"""Regression tests for file-name handling.

This exists because of a real crash, not a hypothetical one. The v4 Identifier
rule only allows `[A-Za-z0-9_.:-]` starting with an alphanumeric, and the
automatic `image_id` used to be the raw file stem - so analysing `照片.png`,
`IMG 1234.JPG`, `截图(1).png` or `_DSC0001.JPG` raised ContractError and the
whole page died. Those are ordinary names from a phone or WeChat, which is
exactly where the photos come from.

The fix sanitises only the automatic path. An explicitly supplied `image_id`
still goes straight into the contract and still raises - the caller owns that
promise, and rewriting it silently would hide a real bug on their side.
"""

from __future__ import annotations

import pytest
from PIL import Image

from core.contract import ContractError, ImageDescriptor
from core.detect import analyze
from core.detect.standin_loader import sanitize_image_id

MAX_LEN = 128


def _is_legal(image_id: str) -> bool:
    """Mirror of the v4 Identifier rule, checked without raising."""
    if not image_id or len(image_id) > MAX_LEN:
        return False
    if not (image_id[0].isascii() and image_id[0].isalnum()):
        return False
    return all(c.isascii() and (c.isalnum() or c in "_.:-") for c in image_id)


# --- names that must survive untouched ------------------------------------


@pytest.mark.parametrize("name", ["express_001", "a.b:c-d_e", "IMG1234", "x"])
def test_already_legal_names_are_not_rewritten(name):
    """No hash for a name that is already legal, or every id in every report changes."""
    assert sanitize_image_id(name) == name


# --- names that used to crash ---------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["照片", "截图(1)", "IMG 1234", "_DSC0001", "🎉party", "user@home", "3月账单", "a b(1)"],
)
def test_real_world_names_become_legal(name):
    out = sanitize_image_id(name)
    assert _is_legal(out), f"{name!r} -> {out!r} is still not a legal Identifier"
    # The contract is the authority, so prove it accepts the output too.
    ImageDescriptor(image_id=out, width=10, height=10)


def test_non_latin_names_stay_distinct():
    """Two Chinese names must not collapse into the same id.

    Replacing every CJK character with `_` alone would map 照片 and 合同 to the
    same string; the CRC suffix is what keeps them apart.
    """
    ids = {sanitize_image_id(n) for n in ("照片", "合同", "截图", "微信图片")}
    assert len(ids) == 4


def test_non_alphanumeric_start_gets_a_prefix():
    """The rule is about the first character, not the whole string."""
    for name in ("_DSC0001", "照片", "🎉party"):
        out = sanitize_image_id(name)
        assert out[0].isascii() and out[0].isalnum(), f"{name!r} -> {out!r}"


def test_overlong_names_are_clipped_but_keep_their_suffix():
    """Truncation must not cut the uniqueness off the end."""
    long_name = "x" * 300
    out = sanitize_image_id(long_name)
    assert len(out) == MAX_LEN
    assert out != sanitize_image_id("x" * 301)


def test_empty_input_still_yields_a_legal_id():
    out = sanitize_image_id("")
    assert _is_legal(out)


# --- end to end ------------------------------------------------------------


def test_analyze_survives_a_chinese_file_name(tmp_path):
    """The exact scenario that used to raise ContractError."""
    path = tmp_path / "照片.png"
    Image.new("RGB", (64, 64), "white").save(path)
    result = analyze(str(path))
    assert result.image_descriptor["image_id"] == sanitize_image_id("照片")
    assert result.image_descriptor["width"] == 64


def test_analyze_survives_a_name_with_spaces_and_brackets(tmp_path):
    path = tmp_path / "IMG 1234 (1).png"
    Image.new("RGB", (64, 64), "white").save(path)
    assert analyze(str(path)).image_descriptor["width"] == 64


def test_explicit_illegal_image_id_still_raises(tmp_path):
    """Sanitising the caller's own id would hide their bug, so it stays strict."""
    path = tmp_path / "ok.png"
    Image.new("RGB", (64, 64), "white").save(path)
    with pytest.raises(ContractError):
        analyze(str(path), image_id="照片")
