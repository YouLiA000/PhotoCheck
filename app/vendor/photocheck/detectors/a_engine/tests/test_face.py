"""K-07 model-path tests: the fallback must not depend on the working directory.

Why this file exists at all: the YuNet path used to be the relative string
`core/detect/models/...`. That works only when the caller happens to run from
the project root. Everywhere else the file is simply "not found", so the
detector degrades to `not_applicable` - and a degraded detector is
indistinguishable from "this photo has no face". A silent wrong answer is
worse than a loud failure, so the path is now resolved against the package
directory and the missing-file case is asserted to raise.

No mediapipe and no real photo needed: these are path and error-contract tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.config import DEFAULT_CONFIG, MODELS_DIR, PACKAGE_ROOT, DetectConfig, FaceConfig
from core.detect import face as face_mod


@pytest.fixture()
def white_image():
    Image = pytest.importorskip("PIL.Image")
    return Image.new("RGB", (640, 480), "white")


def test_default_model_path_is_absolute():
    """A relative default is the bug this file guards against."""
    path = Path(DEFAULT_CONFIG.face.yunet_model_path)
    assert path.is_absolute(), f"model path must be absolute, got {path}"
    assert path.parent == MODELS_DIR, f"{path} is not under {MODELS_DIR}"


def test_model_path_does_not_move_with_the_working_directory(tmp_path, monkeypatch):
    """The same config object, read from a different cwd, must resolve the same."""
    before = Path(DEFAULT_CONFIG.face.yunet_model_path)
    monkeypatch.chdir(tmp_path)
    after = Path(DEFAULT_CONFIG.face.yunet_model_path)
    assert after == before
    assert Path(os.path.normpath(after)) == Path(os.path.normpath(before))
    assert after.parent.is_dir(), "the models directory must exist inside the package"
    assert PACKAGE_ROOT in after.parents


def test_missing_model_is_a_loud_failure(monkeypatch, white_image, tmp_path):
    """No mediapipe and no model file -> raise, with the path in the message.

    Degrading here would report `not_applicable`, which upstream reads as
    "nothing to dispose of". The user would then ship a photo with a face in it.
    """
    monkeypatch.setattr(face_mod, "_probe_mediapipe", lambda cfg: (False, "not installed"))
    cfg = FaceConfig(yunet_model_path=str(tmp_path / "definitely-absent.onnx"))

    with pytest.raises(face_mod.DetectorUnavailable) as excinfo:
        face_mod.detect_faces(white_image, cfg=cfg)

    message = str(excinfo.value)
    assert "face detection unavailable" in message
    assert "definitely-absent.onnx" in message, "the error must name the file it looked for"
    assert "ASCII" in message, "keep the non-ASCII venv hint, it is the most common cause"


def test_available_is_false_when_both_backends_are_missing(monkeypatch, tmp_path):
    """`available()` must reflect reality, not just 'mediapipe importable'."""
    monkeypatch.setattr(face_mod, "mediapipe_available", lambda: False)
    patched = DetectConfig(
        ocr=DEFAULT_CONFIG.ocr,
        qr=DEFAULT_CONFIG.qr,
        face=FaceConfig(yunet_model_path=str(tmp_path / "absent.onnx")),
    )
    monkeypatch.setattr(face_mod, "DEFAULT_CONFIG", patched)
    assert face_mod.available() is False
