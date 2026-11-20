from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

from tools.prepare_face_samples import prepare


class FaceDatasetTests(unittest.TestCase):
    def test_prepared_image_is_anonymous_and_strips_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            image = Image.new("RGB", (40, 60), "white")
            info = PngImagePlugin.PngInfo()
            info.add_text("Author", "private name")
            image.save(source / "private-name.png", pnginfo=info)

            summary = prepare(source, output, detector=lambda _: [])
            with Image.open(output / "images" / "face-dev-001.jpg") as prepared:
                self.assertFalse(prepared.info.get("Author"))
            self.assertEqual(summary["truth_face_count"], 1)
            self.assertFalse(summary["complete_required_count"])
            self.assertNotIn("private-name", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
