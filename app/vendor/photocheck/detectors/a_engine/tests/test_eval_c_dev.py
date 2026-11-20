"""Tests for scripts/eval_c_dev.py - the bridge between C's artefacts and our metrics.

Only the pure parts are covered: parsing C's v4 label arrays, parsing the
manifest's `phone=2;qr=1` counts, bucketing predictions, and picking the split.
The scoring itself is `core.eval`, which already has its own tests.

Why this matters enough to test: these two parsers decide the denominator. A bug
in `load_c_label` that silently drops an object makes recall look better, and a
bug in `parse_manifest_objects` makes the real-photo comparison meaningless -
both are exactly the kind of error that produces a good number with no signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_c_dev  # noqa: E402


# --- manifest parsing -------------------------------------------------------


def test_parse_manifest_objects_basic():
    assert eval_c_dev.parse_manifest_objects("phone=2;qr=1;barcode=1") == {
        "phone": 2,
        "qr": 1,
        "barcode": 1,
    }


def test_parse_manifest_objects_empty_is_empty_not_an_error():
    """A clean photo legitimately has no objects; that is not a parse failure."""
    assert eval_c_dev.parse_manifest_objects("") == {}
    assert eval_c_dev.parse_manifest_objects(None) == {}


def test_parse_manifest_objects_keeps_unknown_keys():
    """An unknown key must surface as unscored, not vanish from the total."""
    assert eval_c_dev.parse_manifest_objects("phone=1;face=2") == {"phone": 1, "face": 2}


def test_parse_manifest_objects_accumulates_duplicates():
    assert eval_c_dev.parse_manifest_objects("phone=1;phone=2") == {"phone": 3}


# --- prediction bucketing ---------------------------------------------------


def _det(kind: str, subtype: str) -> dict:
    return {"kind": kind, "subtype": subtype}


def test_count_predictions_separates_qr_from_barcode():
    """`qr_code` is the 1D barcode landing spot; mixing it into `qr` would inflate QR recall."""
    dets = [
        _det("qr", "qr_wifi"),
        _det("qr", "qr_vcard"),
        _det("qr", "qr_code"),
    ]
    assert eval_c_dev.count_predictions(dets) == {
        "phone": 0,
        "idcard": 0,
        "qr": 2,
        "barcode": 1,
    }


def test_count_predictions_ignores_face_and_meta():
    dets = [_det("face", "face_region"), _det("meta", "gps"), _det("text", "phone")]
    got = eval_c_dev.count_predictions(dets)
    assert got["phone"] == 1
    assert got["qr"] == 0 and got["barcode"] == 0 and got["idcard"] == 0


def test_count_predictions_unknown_qr_subtype_counts_as_qr():
    assert eval_c_dev.count_predictions([_det("qr", "qr_unknown")])["qr"] == 1


# --- C's v4 label array -----------------------------------------------------


def _write_label(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "image-dev-999.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def test_load_c_label_splits_meta_and_keeps_geometry(tmp_path):
    records = [
        {"record_type": "ImageDescriptor", "image_id": "image-dev-999", "width": 800, "height": 600},
        {"record_type": "Detection", "id": "d1", "kind": "text", "subtype": "phone",
         "bbox": [10, 20, 100, 30]},
        {"record_type": "Detection", "id": "d2", "kind": "meta", "subtype": "gps", "bbox": None},
        {"record_type": "Detection", "id": "d3", "kind": "qr", "subtype": "qr_vcard",
         "bbox": [100, 100, 200, 200]},
    ]
    gt, meta = eval_c_dev.load_c_label(_write_label(tmp_path, records))

    assert gt.image_id == "image-dev-999"
    assert (gt.width, gt.height) == (800, 600)
    assert [o.subtype for o in gt.objects] == ["phone", "qr_vcard"]
    assert gt.objects[0].bbox == (10, 20, 100, 30)
    # meta is B's K-01: kept aside for reporting, never in A's denominator.
    assert [m["subtype"] for m in meta] == ["gps"]


def test_load_c_label_drops_non_geometry_text_record_with_a_warning(tmp_path, capsys):
    """A text record without a bbox cannot be paired; it must not silently shrink GT."""
    records = [
        {"record_type": "ImageDescriptor", "image_id": "x", "width": 10, "height": 10},
        {"record_type": "Detection", "id": "bad", "kind": "text", "subtype": "phone", "bbox": None},
    ]
    gt, _ = eval_c_dev.load_c_label(_write_label(tmp_path, records))
    assert gt.objects == ()
    assert gt.negative is True
    assert "no bbox" in capsys.readouterr().err


def test_load_c_label_ignores_kinds_a_does_not_ship(tmp_path):
    records = [
        {"record_type": "ImageDescriptor", "image_id": "x", "width": 10, "height": 10},
        {"record_type": "Detection", "id": "z", "kind": "exif_thumb", "subtype": "thumbnail",
         "bbox": [0, 0, 5, 5]},
    ]
    gt, _ = eval_c_dev.load_c_label(_write_label(tmp_path, records))
    assert gt.objects == ()


def test_load_c_label_requires_an_image_descriptor(tmp_path):
    with pytest.raises(ValueError, match="ImageDescriptor"):
        eval_c_dev.load_c_label(_write_label(tmp_path, [{"record_type": "Detection"}]))


# --- split detection --------------------------------------------------------


def test_pick_split_detects_a_labelled_dir(tmp_path):
    (tmp_path / "labels").mkdir()
    assert eval_c_dev._pick_split(tmp_path, "auto") == "dev"


def test_pick_split_detects_a_real_dir(tmp_path):
    (tmp_path / "manifest.csv").write_text("file,objects\n", encoding="utf-8")
    assert eval_c_dev._pick_split(tmp_path, "auto") == "real"


def test_pick_split_detects_the_dataset_root(tmp_path):
    (tmp_path / "dev").mkdir()
    (tmp_path / "real").mkdir()
    assert eval_c_dev._pick_split(tmp_path, "auto") == "all"


def test_pick_split_explicit_wins(tmp_path):
    (tmp_path / "labels").mkdir()
    assert eval_c_dev._pick_split(tmp_path, "real") == "real"


def test_pick_split_refuses_a_directory_it_cannot_classify(tmp_path):
    with pytest.raises(SystemExit, match="cannot tell"):
        eval_c_dev._pick_split(tmp_path, "auto")


# --- image lookup -----------------------------------------------------------


def test_find_image_prefers_jpg_then_falls_back(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    assert eval_c_dev._find_image(tmp_path, "a").name == "a.jpg"
    assert eval_c_dev._find_image(tmp_path, "b").name == "b.png"
    assert eval_c_dev._find_image(tmp_path, "missing") is None
