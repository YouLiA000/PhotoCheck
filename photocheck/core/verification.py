"""K-09 structural, metadata and detector-backed content verification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from PIL import Image

from photocheck.contract import build_verification_report, validate_records

from .ingest import IngestedImage
from .metadata import MetadataScanError, metadata_residual_tags, read_metadata
from .redaction import AppliedRegion, RedactedImage


class VerificationError(ValueError):
    """Raised when a final output fails a deterministic safety check."""


ContentProbe = Callable[..., Mapping[str, Any]]


def verify_redacted_export(
    ingested: IngestedImage,
    output_path: str | os.PathLike[str],
    plan: Mapping[str, Any],
    redacted: RedactedImage,
    *,
    detections: Iterable[Mapping[str, Any]] = (),
    decisions: Iterable[Mapping[str, Any]] = (),
    exiftool_path: str | os.PathLike[str] | None = None,
    content_probe: ContentProbe | None = None,
) -> dict[str, Any]:
    """Verify a final file and return a v4 VerificationReport."""

    detections = [dict(detection) for detection in detections]
    decisions = [dict(decision) for decision in decisions]
    descriptor = ingested.descriptor
    try:
        validate_records([descriptor, *detections, *decisions, dict(plan)])
    except Exception as exc:
        raise VerificationError(f"invalid workflow records: {exc}") from exc
    if redacted.plan_revision != plan["revision"]:
        raise VerificationError("redacted image revision does not match the plan")

    path = Path(output_path)
    try:
        with Image.open(path) as output:
            output.load()
            if output.size != ingested.image.size:
                raise VerificationError("final output dimensions differ from the working image")
            try:
                metadata = read_metadata(path, exiftool_path=exiftool_path)
            except MetadataScanError as exc:
                raise VerificationError("final metadata scan failed") from exc
            if metadata_residual_tags(metadata):
                raise VerificationError("final output still contains content metadata")

            probe = content_probe or _default_content_probe
            checks = [
                _verify_region(output, region, detections, probe)
                for region in redacted.applied_regions
            ]
    except VerificationError:
        raise
    except (OSError, ValueError) as exc:
        raise VerificationError("final output is not a readable image") from exc

    return build_verification_report(
        descriptor,
        plan,
        detections,
        decisions,
        checks,
    )


def _verify_region(
    output: Image.Image | str | os.PathLike[str],
    region: AppliedRegion,
    detections: list[dict[str, Any]],
    content_probe: ContentProbe,
) -> dict[str, Any]:
    if not isinstance(output, Image.Image):
        try:
            with Image.open(output) as opened:
                opened.load()
                return _verify_region(opened, region, detections, content_probe)
        except (OSError, ValueError):
            return _check(region, "failed", f"a_{region.method}_probe")
    finding = next(
        (
            detection
            for detection in detections
            if detection["id"] == region.finding_id
        ),
        None,
    )
    if finding is not None and finding["kind"] in {"text", "qr", "face"}:
        return _verify_finding(output, region, finding, content_probe)
    if region.method != "solid":
        return {
            "region_id": region.id,
            "status": "not_applicable",
            "method": f"{region.method}_region_structural_only",
        }

    crop = output.crop(_bbox_xyxy(region.applied_bbox)).convert("RGBA")
    if crop.width <= 2 or crop.height <= 2:
        uniform = False
    else:
        interior = crop.crop((1, 1, crop.width - 1, crop.height - 1))
        pixels = np.asarray(interior, dtype=np.int16)
        rgb = pixels[..., :3]
        black_enough = bool(
            float(rgb.mean()) <= 8
            and float(np.percentile(rgb, 99)) <= 24
        )
        alpha_safe = bool(
            pixels.shape[-1] == 3
            or float(np.percentile(pixels[..., 3], 1)) >= 250
        )
        uniform = black_enough and alpha_safe
    return {
        "region_id": region.id,
        "status": "not_applicable" if uniform else "failed",
        "method": "solid_black_region_structural_only",
    }


def _verify_finding(
    output: Image.Image,
    region: AppliedRegion,
    finding: Mapping[str, Any],
    content_probe: ContentProbe,
) -> dict[str, Any]:
    kind = finding["kind"]
    try:
        evidence = content_probe(
            output,
            region.source_bbox,
            kinds=(kind,),
        )
    except Exception:
        return _check(region, "failed", f"a_{kind}_probe")

    unavailable = evidence.get("unavailable", ())
    if kind in unavailable:
        return _check(region, "not_applicable", f"a_{kind}_probe")

    try:
        if kind == "text":
            residual = int(evidence["residual_text"])
            status = "failed" if residual > 0 else "passed"
            return _check(region, status, "a_ocr_probe")

        if kind == "qr":
            detected = int(evidence["residual_qr_detected"])
            decodable = int(evidence["residual_qr_decodable"])
            if detected < 0 or decodable < 0:
                return _check(region, "failed", "a_qr_probe")
            if decodable > detected:
                return _check(region, "failed", "a_qr_probe")
            status = "failed" if detected > 0 or decodable > 0 else "passed"
            return _check(region, status, "a_qr_probe")

        residual = int(evidence["residual_face"])
        status = "failed" if residual > 0 else "passed"
        return _check(region, status, "a_face_probe")
    except (KeyError, TypeError, ValueError):
        return _check(region, "failed", f"a_{kind}_probe")


def _check(
    region: AppliedRegion,
    status: str,
    method: str,
) -> dict[str, str]:
    return {
        "region_id": region.id,
        "status": status,
        "method": method,
    }


def _default_content_probe(
    image: Any,
    bbox: tuple[int, int, int, int],
    *,
    kinds: tuple[str, ...],
) -> Mapping[str, Any]:
    from photocheck.detectors import probe_a_region

    return probe_a_region(image, bbox, kinds=kinds)


def _bbox_xyxy(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, width, height = bbox
    return x, y, x + width, y + height
