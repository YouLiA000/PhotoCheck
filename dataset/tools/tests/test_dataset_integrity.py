"""数据集完整性测试：防止"看起来有、其实没有"的静默损坏。

真实踩过的坑：换成 Pillow 10.4 后，EXIF 子 IFD 写入静默失效——图还是那张图，
GPS 和时间却全丢了。B 拿这种图去测 K-02，会以为是自己的清除逻辑没跑通。

    python -m unittest discover -s dataset/tools/tests -v
"""

from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.validate_records import validate_records  # noqa: E402

DEV = ROOT / "dataset" / "dev"


def manifest() -> list[dict]:
    with open(DEV / "manifest.csv", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def labels(image_id: str) -> list[dict]:
    return json.loads((DEV / "labels" / f"{image_id}.json").read_text(encoding="utf-8"))


class ExifIntegrity(unittest.TestCase):
    def test_exif_present_where_manifest_says(self):
        import piexif

        targets = [m for m in manifest() if m["has_exif"] == "1"]
        self.assertGreaterEqual(len(targets), 5, "带 EXIF 的样本太少，K-01/K-02 没法测")
        for row in targets:
            path = DEV / row["file"]
            data = piexif.load(str(path))
            with self.subTest(image=row["image_id"]):
                self.assertTrue(data["GPS"], "GPS 丢了——B 的元数据清除测试会全部假通过")
                self.assertIn(2, data["GPS"], "缺 GPSLatitude")
                self.assertIn(36867, data["Exif"], "缺 DateTimeOriginal")
                self.assertTrue(data["0th"].get(271), "缺 Make")

    def test_exif_absent_where_manifest_says(self):
        import piexif

        targets = [m for m in manifest() if m["has_exif"] == "0" and m["template"] != "broken"]
        for row in targets:
            data = piexif.load(str(DEV / row["file"]))
            with self.subTest(image=row["image_id"]):
                self.assertFalse(data["GPS"], "不该有 GPS 却写进去了，负样本被污染")


class DatasetConsistency(unittest.TestCase):
    def test_images_openable(self):
        from PIL import Image

        # 坏图本来就打不开（全黑能开、超小能开、非图片与截断 JPEG 不能），必须排除
        for row in manifest():
            if row["template"] == "broken":
                continue
            with Image.open(DEV / row["file"]) as im:
                self.assertEqual(im.size, (int(row["width"]), int(row["height"])))

    def test_labels_pass_contract(self):
        for path in sorted((DEV / "labels").glob("*.json")):
            with self.subTest(image=path.stem):
                report = validate_records(json.loads(path.read_text(encoding="utf-8")))
                self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_manifest_counts_match_labels(self):
        # 坏图按设计不产出标注（不进检测指标），跳过
        for row in manifest():
            if row["template"] == "broken":
                continue
            dets = [r for r in labels(row["image_id"]) if r["record_type"] == "Detection"]
            visual = [d for d in dets if d["kind"] != "meta"]
            meta = [d for d in dets if d["kind"] == "meta"]
            with self.subTest(image=row["image_id"]):
                self.assertEqual(int(row["positives"]), len(visual))
                self.assertEqual(int(row["meta_findings"]), len(meta))

    def test_negatives_only_on_images_that_exist(self):
        neg = json.loads((DEV / "negatives.json").read_text(encoding="utf-8"))
        known = {m["image_id"] for m in manifest()}
        for image_id in neg:
            self.assertIn(image_id, known, f"{image_id} 在 negatives.json 里但不在清单里")

    def test_qr_decodable_truth_matches_reality(self):
        """真值里写的 decodable 必须与实测一致。

        这条路最容易出错：写 decodable=True 但图糊到解不出，评测时会把「检测到了但解不出」
        误算成系统缺陷；反过来写 False 但实际能解，第二层「二维码 100% 不可解」会假通过。
        """
        try:
            import cv2
            from pyzbar.pyzbar import decode as zbar
        except ImportError:
            self.skipTest("未装 opencv / pyzbar")

        checked = 0
        for path in sorted((DEV / "labels").glob("*.json")):
            for rec in labels(path.stem):
                if not str(rec.get("subtype", "")).startswith("qr_"):
                    continue
                img = cv2.imread(str(DEV / "images" / f"{path.stem}.jpg"))
                real = bool(zbar(img))
                with self.subTest(image=path.stem, subtype=rec["subtype"]):
                    self.assertEqual(rec["details"]["decodable"], real,
                                     "真值写的 decodable 与 pyzbar 实测不一致")
                checked += 1
        self.assertGreaterEqual(checked, 5, "二维码样本太少")

    def test_difficulty_meets_spec_floor(self):
        # K-13 四条规范第 2 条：困难样本 ≥ 30%
        rows = [m for m in manifest() if m["template"] != "broken"]
        hard = sum(1 for m in rows if m["difficulty"] == "hard")
        self.assertGreaterEqual(hard / len(rows), 0.30, "困难样本占比跌破 30%")


class LockSet(unittest.TestCase):
    """锁定集的纪律：模板不相交、数量达标、封存记录必须能对上。"""

    LOCK = ROOT / "dataset" / "lock"

    @staticmethod
    def _manifest(folder: Path) -> list[dict]:
        with open(folder / "manifest.csv", encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_lock_exists_and_is_big_enough(self):
        rows = self._manifest(self.LOCK)
        self.assertGreaterEqual(len(rows), 20, "K-13 要求 lock >= 20 张")

    def test_lock_templates_disjoint_from_dev(self):
        # K-13 四条规范第 1 条：同模板必须整组进同一集合。模板不相交才算真锁上。
        dev_tpl = {m["template"] for m in self._manifest(DEV)}
        lock_tpl = {m["template"] for m in self._manifest(self.LOCK)}
        overlap = dev_tpl & lock_tpl
        self.assertEqual(overlap, set(), f"dev 与 lock 共用模板，等于没锁：{overlap}")

    def test_lock_scenes_disjoint_from_dev(self):
        dev_scene = {m["scene_id"] for m in self._manifest(DEV)}
        lock_scene = {m["scene_id"] for m in self._manifest(self.LOCK)}
        self.assertEqual(dev_scene & lock_scene, set())

    def test_lock_labels_pass_contract(self):
        for path in sorted((self.LOCK / "labels").glob("*.json")):
            with self.subTest(image=path.stem):
                report = validate_records(json.loads(path.read_text(encoding="utf-8")))
                self.assertTrue(report.ok, [str(i) for i in report.errors])

    def test_lock_hash_seal_intact(self):
        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import seal_lock

        problems = seal_lock.verify(self.LOCK)
        self.assertEqual(problems, [], "lock 集被改动过；确认是误改还是有意为之，再重新 seal")


class RuleCoverage(unittest.TestCase):
    """规则写了但数据覆盖不到，等于没写。K-10 的两条组合加成必须至少能演示一条。"""

    def test_at_least_one_sample_triggers_a_bonus(self):
        from contract.risk_explain import explain

        hits = []
        for path in sorted((DEV / "labels").glob("*.json")):
            records = json.loads(path.read_text(encoding="utf-8"))
            exp = explain([r for r in records if r["record_type"] == "Detection"])
            if exp.bonuses:
                hits.append((path.stem, exp.score))
        self.assertGreaterEqual(
            len(hits), 1,
            "全数据集没有任何一张图能触发组合加成——规则写了但演示不出来，评审问起就是空的",
        )

    def test_face_bonus_is_documented_as_uncovered(self):
        # GPS + 人脸 +2 无法用现有数据演示：数据集里没有人脸样本（K-07 未做）。
        # 这条测试的作用是**让缺口显式存在**，而不是假装覆盖了。
        from contract.risk_rules import BONUS_RULES

        self.assertIn(("gps", "face_region", 2), BONUS_RULES)
        faces = sum(
            1 for p in (DEV / "labels").glob("*.json")
            for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("kind") == "face"
        )
        self.assertEqual(faces, 0, "如果补了人脸样本，这条测试要跟着改，并补上加成覆盖")




class NearDuplicate(unittest.TestCase):
    """锁集独立性要**量化**，不能只靠"模板名不同"。

    模板名不同不等于图不像：同一套版式骨架换个标题，长得很可能很像。
    """

    def test_no_near_duplicate_across_dev_and_lock(self):
        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import near_duplicate

        dev = near_duplicate.collect(DEV)
        lock = near_duplicate.collect(ROOT / "dataset" / "lock")
        self.assertTrue(dev and lock, "两套都要有图才能比")
        pairs = near_duplicate.compare(dev, lock, cross=True)
        near = [p for p in pairs if p["hamming"] <= near_duplicate.NEAR_DUP_MAX]
        self.assertEqual(
            near, [],
            f"dev 与 lock 之间有 {len(near)} 对近重复，锁集独立性存疑：{near[:2]}",
        )

    def test_within_set_is_more_similar_than_across(self):
        # 同模板同版式 → 汉明距离接近 0；跨模板应当明显更远。
        # 这条失败通常意味着"跨集比同集还像"，也就是锁集选错了。
        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import near_duplicate

        dev = near_duplicate.collect(DEV)
        lock = near_duplicate.collect(ROOT / "dataset" / "lock")
        cross = min(p["hamming"] for p in near_duplicate.compare(dev, lock, cross=True))
        within = min(p["hamming"] for p in near_duplicate.compare(dev, dev, cross=False))
        self.assertLess(within, cross, "同集内应比跨集更像（同模板版式一致）")




class SealScoping(unittest.TestCase):
    """封存函数必须真的认它收到的目录。

    踩过一次：`SUMS` / `SEAL` 是模块级常量，`seal(其它目录)` 会把结果写进**真实 lock**
    的封存记录，`verify(其它目录)` 其实在校验真实 lock。签名说支持传目录、实现却不认，
    比不写这个参数更危险——调用方以为自己隔离了，实际动了真数据。
    """

    def _fake_lock(self, tmp: Path) -> Path:
        root = tmp / "lock"
        (root / "images").mkdir(parents=True)
        (root / "labels").mkdir()
        (root / "images" / "a.jpg").write_bytes(b"fake-jpeg-bytes")
        (root / "labels" / "a.json").write_text("[]", encoding="utf-8")
        return root

    def test_seal_and_verify_stay_inside_given_dir(self):
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import seal_lock

        real_sums = seal_lock.sums_path(seal_lock.LOCK)
        before = real_sums.read_bytes() if real_sums.exists() else b""

        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_lock(Path(tmp))
            seal_lock.seal(fake)
            self.assertTrue((fake / "SHA256SUMS.txt").exists(), "封存记录没写进目标目录")
            self.assertEqual(seal_lock.verify(fake), [])
            (fake / "images" / "a.jpg").write_bytes(b"changed")
            self.assertTrue(any("被改动" in p for p in seal_lock.verify(fake)))

        after = real_sums.read_bytes() if real_sums.exists() else b""
        self.assertEqual(before, after, "封存别的目录时动了真实 lock 的记录")

    def test_malformed_sums_line_is_reported_not_crashed(self):
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import seal_lock

        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_lock(Path(tmp))
            (fake / "SHA256SUMS.txt").write_text("这不是哈希行\n", encoding="utf-8")
            problems = seal_lock.verify(fake)   # 不能抛异常
            self.assertTrue(any("格式不合法" in p for p in problems), problems)

    def test_missing_seal_file_is_reported(self):
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import seal_lock

        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_lock(Path(tmp))
            seal_lock.seal(fake)
            seal_lock.seal_path(fake).unlink()
            problems = seal_lock.verify(fake)
            self.assertTrue(any("SEALED.json" in p for p in problems), problems)

    def test_seal_file_without_sums_hash_is_reported(self):
        import json
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(ROOT / "dataset" / "tools"))
        import seal_lock

        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_lock(Path(tmp))
            seal_lock.seal(fake)
            meta = json.loads(seal_lock.seal_path(fake).read_text(encoding="utf-8"))
            meta.pop("sums_sha256")
            seal_lock.seal_path(fake).write_text(
                json.dumps(meta), encoding="utf-8", newline="\n"
            )
            problems = seal_lock.verify(fake)
            self.assertTrue(any("sums_sha256" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
