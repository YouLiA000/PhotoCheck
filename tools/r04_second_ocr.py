#!/usr/bin/env python3
"""R-04: independent Tesseract cross-check and a 30-object manual review packet."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photocheck.contract import build_redaction_plan, validate_records  # noqa: E402
from photocheck.core import ingest_image, process_and_export  # noqa: E402
from photocheck.core.metadata import resolve_exiftool  # noqa: E402
from photocheck.core.redaction import expand_bbox  # noqa: E402
from photocheck.detectors import analyze_a  # noqa: E402

SUPPORTED_SUBTYPES = ("phone", "idcard")
TESSERACT_CANDIDATES = (
    r"D:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"D:\Tesseract-OCR\tesseract.exe",
)


@dataclass(frozen=True)
class Candidate:
    image_id: str
    detection_id: str
    subtype: str
    bbox: tuple[int, int, int, int]
    masked_preview: str
    image_path: Path


def resolve_tesseract(explicit: str | None = None) -> Path:
    candidates: list[str | None] = [explicit, shutil.which("tesseract"), *TESSERACT_CANDIDATES]
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value)
    raise FileNotFoundError(
        "Tesseract was not found; expected D:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    )


def tesseract_versions(executable: Path) -> tuple[str, list[str]]:
    version_run = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=20,
        encoding="utf-8",
        errors="replace",
    )
    language_run = subprocess.run(
        [str(executable), "--list-langs"],
        capture_output=True,
        text=True,
        timeout=20,
        encoding="utf-8",
        errors="replace",
    )
    if version_run.returncode != 0 or language_run.returncode != 0:
        raise RuntimeError("Tesseract version or language probe failed")
    first_line = (version_run.stdout or version_run.stderr).splitlines()[0]
    version = first_line.split()[1] if len(first_line.split()) > 1 else first_line
    languages = [
        line.strip()
        for line in language_run.stdout.splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    ]
    return version, languages


def load_candidates(dataset_root: Path, split: str = "dev") -> list[Candidate]:
    split_root = dataset_root / split
    label_root = split_root / "labels"
    image_root = split_root / "images"
    candidates: list[Candidate] = []
    for label_path in sorted(label_root.glob("*.json")):
        records = json.loads(label_path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            continue
        image_path = image_root / f"{label_path.stem}.jpg"
        if not image_path.is_file():
            continue
        for record in records:
            if (
                record.get("record_type") != "Detection"
                or record.get("kind") != "text"
                or record.get("subtype") not in SUPPORTED_SUBTYPES
                or not isinstance(record.get("bbox"), list)
            ):
                continue
            preview = (record.get("details") or {}).get("masked_preview")
            candidates.append(
                Candidate(
                    image_id=str(record["image_id"]),
                    detection_id=str(record["id"]),
                    subtype=str(record["subtype"]),
                    bbox=tuple(int(value) for value in record["bbox"]),
                    masked_preview=str(preview or "**"),
                    image_path=image_path,
                )
            )
    return candidates


def stratified_sample(
    candidates: Iterable[Candidate],
    *,
    phone_count: int,
    idcard_count: int,
    seed: int,
) -> list[Candidate]:
    rng = random.Random(seed)
    selected: list[Candidate] = []
    for subtype, count in (("phone", phone_count), ("idcard", idcard_count)):
        pool = sorted(
            (item for item in candidates if item.subtype == subtype),
            key=lambda item: (item.image_id, item.detection_id),
        )
        if len(pool) < count:
            raise ValueError(f"not enough {subtype} candidates: need {count}, have {len(pool)}")
        selected.extend(rng.sample(pool, count))
    return sorted(selected, key=lambda item: (item.image_id, item.bbox[1], item.bbox[0]))


def bbox_iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    x1, y1 = max(lx, rx), max(ly, ry)
    x2, y2 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    if not intersection:
        return 0.0
    union = lw * lh + rw * rh - intersection
    return intersection / max(1, union)


def best_detection(candidate: Candidate, detections: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [
        (
            bbox_iou(candidate.bbox, tuple(int(value) for value in detection["bbox"])),
            detection,
        )
        for detection in detections
        if detection.get("kind") == "text"
        and detection.get("subtype") == candidate.subtype
        and isinstance(detection.get("bbox"), list)
    ]
    if not matches:
        return None
    score, detection = max(matches, key=lambda item: item[0])
    return detection if score >= 0.5 else None


def tesseract_reading(
    image: Image.Image,
    *,
    executable: Path,
    language: str = "chi_sim+eng",
    confidence: int = 50,
) -> dict[str, Any]:
    import pytesseract
    from pytesseract import Output

    pytesseract.pytesseract.tesseract_cmd = str(executable)
    prepared = image.convert("RGB")
    if prepared.width < 240:
        scale = max(2, round(240 / max(1, prepared.width)))
        prepared = prepared.resize(
            (prepared.width * scale, prepared.height * scale),
            Image.Resampling.LANCZOS,
        )
    data = pytesseract.image_to_data(
        prepared,
        lang=language,
        config="--psm 7",
        output_type=Output.DICT,
        timeout=20,
    )
    non_space_chars = 0
    accepted_confidences: list[float] = []
    for text, raw_confidence in zip(data.get("text", []), data.get("conf", [])):
        try:
            score = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        token = re.sub(r"\s+", "", str(text or ""))
        if token and score >= confidence:
            non_space_chars += len(token)
            accepted_confidences.append(score)
    return {
        "chars": non_space_chars,
        "readable": non_space_chars > 0,
        "max_confidence": round(max(accepted_confidences), 1) if accepted_confidences else None,
    }


def _crop_for_bbox(image: Image.Image, bbox: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = bbox
    return image.crop((x, y, x + width, y + height)).convert("RGB")


def _make_contact_sheet(
    crops: list[tuple[str, str, Image.Image]],
    destination: Path,
) -> None:
    columns = 5
    cell_width, cell_height = 360, 120
    rows = max(1, (len(crops) + columns - 1) // columns)
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (image_id, preview, crop) in enumerate(crops):
        column = index % columns
        row = index // columns
        x0, y0 = column * cell_width, row * cell_height
        draw.rectangle((x0 + 2, y0 + 2, x0 + cell_width - 3, y0 + cell_height - 3), outline="black")
        draw.text((x0 + 8, y0 + 8), f"{index + 1:02d} {image_id} {preview}", fill="black", font=font)
        crop.thumbnail((cell_width - 20, cell_height - 38), Image.Resampling.LANCZOS)
        sheet.paste(crop, (x0 + 10, y0 + 32))
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG")


def run(
    *,
    dataset_root: Path,
    output_dir: Path,
    sample_size: int,
    phone_count: int,
    idcard_count: int,
    seed: int,
    tesseract: Path,
    language: str,
    confidence: int,
    exiftool: str | None,
) -> dict[str, Any]:
    if sample_size != phone_count + idcard_count:
        raise ValueError("sample size must equal phone_count + idcard_count")
    if not 0 <= confidence <= 100:
        raise ValueError("confidence must be between 0 and 100")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be new or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    crop_root = output_dir / "manual_crops"
    crop_root.mkdir()
    control_root = output_dir / "control_crops"
    control_root.mkdir()
    processed_root = output_dir / "processed"
    processed_root.mkdir()

    tesseract_version, languages = tesseract_versions(tesseract)
    if "chi_sim" not in languages:
        raise RuntimeError("Tesseract is installed but chi_sim is missing")

    candidates = load_candidates(dataset_root)
    sampled = stratified_sample(
        candidates,
        phone_count=phone_count,
        idcard_count=idcard_count,
        seed=seed,
    )
    by_image: dict[str, list[Candidate]] = {}
    for candidate in sampled:
        by_image.setdefault(candidate.image_id, []).append(candidate)

    rows: list[dict[str, Any]] = []
    contacts: list[tuple[str, str, Image.Image]] = []
    controls: list[tuple[str, str, Image.Image]] = []
    started_at = datetime.now().isoformat(timespec="seconds")
    for image_id, image_candidates in sorted(by_image.items()):
        candidate = image_candidates[0]
        with ingest_image(candidate.image_path, image_id=image_id, exiftool_path=exiftool) as ingested:
            envelope = analyze_a(ingested.image, image_id, kinds=("text",))
            detections = [
                dict(record) for record in (*ingested.detections, *envelope.detections)
            ]
            decisions = [
                {
                    "contract_version": "photocheck/v4",
                    "record_type": "Decision",
                    "finding_id": detection["id"],
                    "action": "redact",
                    "revision": 1,
                }
                for detection in detections
                if detection["kind"] != "meta"
            ]
            validate_records([ingested.descriptor, *detections, *decisions])
            plan = build_redaction_plan(
                ingested.descriptor,
                detections,
                decisions,
                revision=1,
            )
            result = process_and_export(
                ingested,
                plan,
                output_dir=processed_root,
                detections=detections,
                decisions=decisions,
                exiftool_path=exiftool,
                run_full_recheck=False,
            )
            checks = {
                item["region_id"]: item["status"]
                for item in result.verification.get("checks", [])
            }
            with Image.open(result.export.output_path) as exported:
                exported.load()
                exported_rgb = exported.convert("RGB")
                with Image.open(candidate.image_path) as original_raw:
                    original_rgb = ImageOps.exif_transpose(original_raw).convert("RGB")
                    for item in image_candidates:
                        prediction = best_detection(item, detections)
                        if prediction is None:
                            raise RuntimeError(
                                f"sampled object {item.detection_id} has no matching primary detection"
                            )
                        applied_bbox = expand_bbox(
                            tuple(int(value) for value in prediction["bbox"]),
                            (ingested.descriptor["width"], ingested.descriptor["height"]),
                        )
                        processed_crop = _crop_for_bbox(exported_rgb, applied_bbox)
                        original_crop = _crop_for_bbox(original_rgb, applied_bbox)
                        tesseract_result = tesseract_reading(
                            processed_crop,
                            executable=tesseract,
                            language=language,
                            confidence=confidence,
                        )
                        control_result = tesseract_reading(
                            original_crop,
                            executable=tesseract,
                            language=language,
                            confidence=confidence,
                        )
                        primary_status = checks.get(f"redact:{prediction['id']}", "missing")
                        tesseract_status = "failed" if tesseract_result["readable"] else "passed"
                        rows.append(
                            {
                                "sample_no": len(rows) + 1,
                                "image_id": image_id,
                                "detection_id": prediction["id"],
                                "label_detection_id": item.detection_id,
                                "subtype": item.subtype,
                                "masked_preview": prediction.get("details", {}).get(
                                    "masked_preview", item.masked_preview
                                ),
                                "bbox": ",".join(str(value) for value in applied_bbox),
                                "primary_status": primary_status,
                                "tesseract_status": tesseract_status,
                                "tesseract_confidence_threshold": confidence,
                                "tesseract_residual_chars": tesseract_result["chars"],
                                "tesseract_max_confidence": tesseract_result["max_confidence"],
                                "control_read_chars": control_result["chars"],
                                "control_readable": int(control_result["readable"]),
                                "control_max_confidence": control_result["max_confidence"],
                                "agreement": int(primary_status == tesseract_status),
                                "manual_reviewer_a": "",
                                "manual_reviewer_b": "",
                                "manual_adjudicated": "",
                                "notes": "",
                            }
                        )
                        crop_path = crop_root / f"{len(rows):02d}.png"
                        processed_crop.save(crop_path, format="PNG")
                        control_path = control_root / f"{len(rows):02d}.png"
                        original_crop.save(control_path, format="PNG")
                        contacts.append(
                            (image_id, str(rows[-1]["masked_preview"]), processed_crop.copy())
                        )
                        controls.append(
                            (image_id, str(rows[-1]["masked_preview"]), original_crop.copy())
                        )
                        processed_crop.close()
                        original_crop.close()

    if len(rows) != sample_size:
        raise RuntimeError(f"expected {sample_size} processed objects, got {len(rows)}")
    _make_contact_sheet(contacts, output_dir / "manual-contact-sheet.png")
    _make_contact_sheet(controls, output_dir / "control-contact-sheet.png")
    for crop in contacts:
        crop[2].close()
    for crop in controls:
        crop[2].close()

    csv_path = output_dir / "r04-objects.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    primary_passed = sum(row["primary_status"] == "passed" for row in rows)
    tesseract_passed = sum(row["tesseract_status"] == "passed" for row in rows)
    agreements = sum(bool(row["agreement"]) for row in rows)
    controls_readable = sum(bool(row["control_readable"]) for row in rows)
    payload = {
        "card": "R-04",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": started_at,
        "dataset_root": str(dataset_root.resolve()),
        "sample_size": sample_size,
        "seed": seed,
        "subtype_counts": {"phone": phone_count, "idcard": idcard_count},
        "tesseract": {
            "path": str(tesseract),
            "version": tesseract_version,
            "languages": languages,
            "language_config": language,
            "confidence_threshold": confidence,
        },
        "exiftool": exiftool,
        "summary": {
            "primary_passed": primary_passed,
            "tesseract_passed": tesseract_passed,
            "agreement_count": agreements,
            "agreement_rate": round(agreements / sample_size, 4),
            "control_readable": controls_readable,
            "control_readable_rate": round(controls_readable / sample_size, 4),
            "manual_review_status": "pending_two_independent_human_reviewers",
        },
        "privacy": {
            "raw_ocr_text_stored": False,
            "masked_preview_only": True,
            "processed_crops_only_for_manual_review": True,
            "control_crops_local_only": True,
        },
    }
    (output_dir / "r04-results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "README.md").write_text(
        "# R-04 人工抽检包\n\n"
        f"- 对象：{sample_size} 个（手机号 {phone_count} / 身份证 {idcard_count}）\n"
        f"- 随机种子：{seed}\n"
        "- 查看 `manual-contact-sheet.png` 或 `manual_crops/01.png` 至 `30.png`。\n"
        "- 同时查看 `control-contact-sheet.png` / `control_crops/`，先确认原区域有文字，"
        "再看遮后区域是否仍可读。两份材料都不应提交到公共仓库。\n"
        "- 两位评审独立填写 `r04-objects.csv` 的 `manual_reviewer_a`、"
        "`manual_reviewer_b`，只填 `readable` 或 `not_readable`。\n"
        "- 分歧由第三人填写 `manual_adjudicated`；不得互相讨论后抄同一答案。\n"
        "- `processed/` 仅保存已处理的最终导出图；不保存第二 OCR 的识别明文。\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R-04 second OCR cross-check")
    parser.add_argument("--dataset-root", type=Path, default=ROOT / "dataset")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=30)
    parser.add_argument("--phone", type=int, default=20)
    parser.add_argument("--idcard", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument("--tesseract", default=None)
    parser.add_argument("--language", default="chi_sim+eng")
    parser.add_argument("--confidence", type=int, default=50)
    parser.add_argument("--exiftool", default=None)
    args = parser.parse_args(argv)
    try:
        tesseract = resolve_tesseract(args.tesseract)
        exiftool = str(resolve_exiftool(args.exiftool))
        payload = run(
            dataset_root=args.dataset_root,
            output_dir=args.output_dir,
            sample_size=args.sample_size,
            phone_count=args.phone,
            idcard_count=args.idcard,
            seed=args.seed,
            tesseract=tesseract,
            language=args.language,
            confidence=args.confidence,
            exiftool=exiftool,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[R-04] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
