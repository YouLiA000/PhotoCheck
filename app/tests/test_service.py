from __future__ import annotations

import os
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from app import service


class FakeIngested:
    def __init__(self, path: Path) -> None:
        self.descriptor = {
            "image_id": f"image-{path.stem}",
            "width": 100,
            "height": 80,
        }
        self.image = object()
        self.detections = ()

    def __enter__(self) -> "FakeIngested":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class ServiceUploadTests(unittest.TestCase):
    def test_empty_upload_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "空"):
            service.save_upload("empty.png", b"")

    def test_webp_is_rejected_consistently(self) -> None:
        payload = BytesIO()
        Image.new("RGB", (8, 8), "white").save(payload, format="WEBP")
        with self.assertRaisesRegex(ValueError, "JPG / PNG"):
            service.save_upload("photo.webp", payload.getvalue())

    def test_extension_comes_from_actual_format(self) -> None:
        payload = BytesIO()
        Image.new("RGB", (8, 8), "white").save(payload, format="PNG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(service, "UPLOADS", Path(tmp)):
            source_id = service.save_upload("mislabeled.jpg", payload.getvalue())
            self.assertTrue(service.resolve_source(source_id).name.endswith(".png"))

    def test_stale_work_is_purged_but_recent_work_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            outputs = root / "outputs"
            uploads.mkdir()
            outputs.mkdir()
            old_upload = uploads / "old.jpg"
            new_upload = uploads / "new.jpg"
            old_output = outputs / "run-old"
            old_upload.write_bytes(b"old")
            new_upload.write_bytes(b"new")
            old_output.mkdir()
            old_time = time.time() - 48 * 3600
            os.utime(old_upload, (old_time, old_time))
            os.utime(old_output, (old_time, old_time))

            with (
                mock.patch.object(service, "UPLOADS", uploads),
                mock.patch.object(service, "OUTPUTS", outputs),
            ):
                removed = service.purge_stale_work(24)
            self.assertEqual(removed, 2)
            self.assertFalse(old_upload.exists())
            self.assertFalse(old_output.exists())
            self.assertTrue(new_upload.exists())


class ServiceAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        service.SESSION.clear()

    def tearDown(self) -> None:
        service.SESSION.clear()

    def test_non_finite_manual_box_is_rejected(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service._clamp_manual_box({"bbox": [0, 0, value, 10]}, 100, 100)

    def test_warmup_failure_is_recorded_without_exception_text(self) -> None:
        with mock.patch.object(
            service,
            "analyze_a",
            side_effect=RuntimeError("private value " + "138" + "0013" + "8000"),
        ):
            service.warm_up()
        self.assertFalse(service.SESSION.warm)
        self.assertEqual(service.SESSION.warm_warning, "预热失败：RuntimeError")
        self.assertNotIn("138" + "0013" + "8000", service.status()["warm_warning"])

    def test_server_allocates_revision_from_decision_fingerprint(self) -> None:
        analysis = service.Analysis(
            id="analysis-rev",
            source_id="sample:rev",
            path=Path("unused.jpg"),
            descriptor={},
            detections=(),
            timings={},
            warnings=(),
        )
        service.SESSION.add(analysis)
        self.assertEqual(service.SESSION.allocate_revision("analysis-rev", "first"), 1)
        self.assertEqual(service.SESSION.allocate_revision("analysis-rev", "first"), 1)
        self.assertEqual(service.SESSION.allocate_revision("analysis-rev", "second"), 2)

    def test_analysis_ids_isolate_processing_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            outputs.mkdir()

            def resolve(source_id: str) -> Path:
                return root / f"{source_id.rsplit(':', 1)[1]}.jpg"

            def analyze(image: object, image_id: str) -> SimpleNamespace:
                return SimpleNamespace(detections=[], timings_ms={}, warnings=[])

            ingest_calls: list[dict[str, object]] = []

            def export(ingested: object, plan: dict, **kwargs: object) -> SimpleNamespace:
                output = Path(kwargs["output_dir"]) / f"{plan.get('image_id', 'image')}.jpg"
                output.write_bytes(b"generated")
                return SimpleNamespace(
                    export=SimpleNamespace(
                        output_path=str(output), output_sha256="out", source_sha256="source",
                        source_format="JPEG", pixel_mean_abs_diff=0,
                    ),
                    verification={"disposal_state": "not_applicable", "checks": [], "residuals": []},
                    full_recheck={"detections": [], "warnings": []},
                )

            with (
                mock.patch.object(service, "OUTPUTS", outputs),
                mock.patch.object(service, "resolve_source", side_effect=resolve),
                mock.patch.object(
                    service,
                    "ingest_image",
                    side_effect=lambda path, **kwargs: (
                        ingest_calls.append(kwargs) or FakeIngested(path)
                    ),
                ),
                mock.patch.object(service, "analyze_a", side_effect=analyze),
                mock.patch.object(
                    service,
                    "build_redaction_plan",
                    side_effect=lambda image, *a, **k: {
                        "items": [],
                        "image_id": image["image_id"],
                        "revision": k["revision"],
                    },
                ),
                mock.patch.object(service, "process_and_export", side_effect=export),
            ):
                first = service.detect("sample:first")
                second = service.detect("sample:second")
                self.assertNotEqual(first["analysis_id"], second["analysis_id"])
                report = service.process(
                    "sample:first", [], analysis_id=first["analysis_id"], revision=3
                )
                self.assertEqual(report["analysis_id"], first["analysis_id"])
                self.assertEqual(report["plan_revision"], 3)
                self.assertEqual(report["plan_regions"], [])
                self.assertIn(f"analysis={first['analysis_id']}", report["output_url"])
                self.assertEqual(
                    service.output_image(first["analysis_id"]).name,
                    "image-first.jpg",
                )
                self.assertTrue(ingest_calls)
                self.assertTrue(
                    all(call.get("exiftool_path") == service._EXIFTOOL for call in ingest_calls)
                )
                first_output = service.output_image(first["analysis_id"])
                superseded = service.process(
                    "sample:first", [], analysis_id=first["analysis_id"], revision=4
                )
                self.assertEqual(superseded["plan_revision"], 4)
                self.assertFalse(first_output.parent.exists())
                with self.assertRaises(FileNotFoundError):
                    service.output_image(second["analysis_id"])
                with self.assertRaisesRegex(RuntimeError, "计划版本已过期"):
                    service.process(
                        "sample:first", [], analysis_id=first["analysis_id"], revision=2
                    )


if __name__ == "__main__":
    unittest.main()
