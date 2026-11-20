"""Integrity checks for the committed K-13 samples.

These are deliberately static: they never run a detector, so they stay fast and
cannot fail because a model file moved. Their job is to stop the sample set from
rotting around the edges - a GT file whose image was renamed, a dimension that no
longer matches, or a `known_gap` pointing at a badcase id that does not exist.

That last one is the real reason this file exists. `known_gap` exempts a miss
from the CI gate, so a dangling id would turn "documented decision" into "silent
suppression", which is exactly the failure mode the mechanism was built to avoid.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "synthetic"
GT = ROOT / "samples" / "gt"
INDEX = ROOT / "badcases" / "index.json"


def _gt_files() -> list[Path]:
    return sorted(GT.glob("*.json"))


def test_sample_count_matches_k13_target():
    """K-13 asks for 10 samples; the count is a deliverable, not a detail."""
    images = sorted(SAMPLES.glob("*.png"))
    assert len(images) == 10, f"expected 10 K-13 samples, found {[p.name for p in images]}"
    assert len(_gt_files()) == 10


def test_every_gt_has_an_image_with_matching_dimensions():
    from PIL import Image

    for gt_path in _gt_files():
        doc = json.loads(gt_path.read_text(encoding="utf-8"))
        image_path = SAMPLES / doc["image"]
        assert image_path.is_file(), f"{gt_path.name} points at missing image {doc['image']}"
        with Image.open(image_path) as im:
            assert im.size == (doc["width"], doc["height"]), (
                f"{gt_path.name}: GT says {doc['width']}x{doc['height']}, "
                f"image is {im.size[0]}x{im.size[1]}"
            )


def test_gt_holds_no_plaintext_digits():
    """The GT lives in the repo, so it must never carry the numbers themselves."""
    for gt_path in _gt_files():
        raw = gt_path.read_text(encoding="utf-8")
        for digits in ("13800138000", "13900139000", "110101199003070011"):
            assert digits not in raw, f"{gt_path.name} leaks plaintext {digits}"


def test_dropped_idcard_sample_is_labelled_negative():
    """Checksum passes but the date is impossible -> the rule drops it.

    GT is empty on purpose. If someone removes the birth-date check later, this
    image turns into a false alarm instead of a silent behaviour change.
    """
    doc = json.loads((GT / "idcard_bad_date_001.json").read_text(encoding="utf-8"))
    assert doc["objects"] == []
    assert doc["negative"] is True


def test_half_code_sample_declares_its_gap():
    doc = json.loads((GT / "qr_half_001.json").read_text(encoding="utf-8"))
    gaps = [o.get("known_gap") for o in doc["objects"] if o.get("known_gap")]
    assert gaps == ["BC-0011"], "the half-code sample must stay registered under its badcase"


def test_known_gap_ids_refer_to_real_badcases():
    """A dangling id would exempt a miss from the CI gate with no paper trail."""
    known = {item["id"] for item in json.loads(INDEX.read_text(encoding="utf-8"))["items"]}
    for gt_path in _gt_files():
        doc = json.loads(gt_path.read_text(encoding="utf-8"))
        for obj in doc["objects"]:
            gap = obj.get("known_gap")
            if gap:
                assert gap in known, f"{gt_path.name}: known_gap {gap} is not in badcases/index.json"


def _sample_module():
    """Load make_sample.py so the test asserts the *generator's* constants.

    Copying the numbers in here would let the two drift, and then the sample on
    disk would stop matching what the test believes it contains.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_sample", ROOT / "scripts" / "make_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_idcard_numbers_cover_the_three_states():
    """Pure rule check on the committed numbers - no OCR, no model.

    The three ID variants exist to separate pass / fail / dropped, so the numbers
    behind them are asserted directly instead of through a detector run.
    """
    from core.detect.entities import idcard_birth_date_valid, idcard_check, scan_text

    m = _sample_module()

    assert idcard_check(m.FAKE_ID) == "pass"
    assert idcard_birth_date_valid(m.FAKE_ID) is True

    assert idcard_check(m.FAKE_ID_X) == "pass"  # last char X, about 1 in 11 real cards
    assert idcard_birth_date_valid(m.FAKE_ID_X) is True

    assert idcard_check(m.FAKE_ID_BAD_DATE) == "pass"  # checksum valid on purpose
    assert idcard_birth_date_valid(m.FAKE_ID_BAD_DATE) is False

    assert idcard_check(m.FAKE_ID_BAD_CHECK) == "fail"
    assert idcard_birth_date_valid(m.FAKE_ID_BAD_CHECK) is True

    # Rule chain: the impossible-date one disappears, the mistyped one is kept.
    assert scan_text(m.FAKE_ID_BAD_DATE) == []
    kept = scan_text(m.FAKE_ID_BAD_CHECK)
    assert len(kept) == 1
    assert kept[0].details["idcard_check"] == "fail"
    assert kept[0].risk == 2


def test_country_code_phones_cover_the_whole_number():
    """`+86 138...` must be one match whose range starts at the country code.

    If the range started after `+86`, the redaction box would leave the country
    code on the image - the number would look masked and still be dialable. The
    masked preview is asserted separately because stripping the code before
    masking is what keeps the subscriber prefix hidden.
    """
    from core.detect.entities import find_entities, mask_phone

    m = _sample_module()
    for raw, expected_preview in (
        (m.FAKE_PHONE_INTL, "138****8000"),
        (m.FAKE_PHONE_0086, "139****9000"),
    ):
        matches = find_entities(raw)
        assert len(matches) == 1, f"{raw} was not recognised as exactly one entity"
        assert matches[0].start == 0, "the match must start at the country code, not after it"
        assert matches[0].end == len(raw) - 1, "the match must run to the last digit"
        assert matches[0].subtype == "phone"
        assert mask_phone(raw) == expected_preview
