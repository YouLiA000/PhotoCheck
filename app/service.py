"""业务编排：把「载入 → 检测 → 决定 → 计划 → 处置 → 复检 → 导出」串成一次调用。

这一层是三个人的交汇点，各自贡献一段：

    A  检测器（K-03~K-07）  →  ``analyze_a`` 给出 Detection
    B  载入/处置/复检/导出   →  ``ingest_image`` / ``build_redaction_plan`` / ``process_and_export``
    C  规则、掩码、Decision  →  ``masked_preview`` 口径、勾选语义、交接包格式

**所有路径都相对本文件**，不引用 C 的仓库。这是能在别人电脑上跑起来的前提。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
VENDOR = APP_ROOT / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from photocheck.contract import build_redaction_plan  # noqa: E402
from photocheck.core import ingest_image, process_and_export  # noqa: E402
from photocheck.detectors import analyze_a  # noqa: E402

SAMPLES_DIR = APP_ROOT / "samples"
SAMPLE_MANIFEST = SAMPLES_DIR / "samples.json"


def _work_root() -> Path:
    """输出目录：优先放在程序同级的 ``work/``（便携，随包走）；
    不可写时退到系统临时目录（装在只读位置也能跑）。"""
    candidates = [APP_ROOT.parent / "work", Path(tempfile.gettempdir()) / "photocheck-work"]
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="photocheck-"))


WORK = _work_root()
UPLOADS = WORK / "uploads"
OUTPUTS = WORK / "outputs"
for _p in (UPLOADS, OUTPUTS):
    _p.mkdir(parents=True, exist_ok=True)


def exiftool_path() -> Path | None:
    """ExifTool 是可选的（没有就跳过独立复核），有就优先用随包的。

    B 的 ``process_and_export`` 会用它做导出后的独立元数据复核——这是报告里
    「元数据残留 0」那一行的来源。缺了它报告会退化成只看 Pillow 的读取。
    """
    for base in (APP_ROOT.parent / "runtime" / "exiftool", APP_ROOT.parent / "tools" / "exiftool"):
        exe = base / "exiftool.exe"
        if exe.is_file():
            return exe
    return None


_EXIFTOOL = exiftool_path()
_DETECTOR_LOCK = threading.Lock()


@dataclass(frozen=True)
class Analysis:
    """一次检测的不可变快照；不同标签页通过 ID 各取各的结果。"""

    id: str
    source_id: str
    path: Path
    descriptor: dict[str, Any]
    detections: tuple[dict[str, Any], ...]
    timings: dict[str, float]
    warnings: tuple[str, ...]


class Session:
    """本机多标签页共享的短期索引，不保存任何检测明文。"""

    def __init__(self, max_analyses: int = 8) -> None:
        self.lock = threading.Lock()
        self.max_analyses = max_analyses
        self.analyses: OrderedDict[str, Analysis] = OrderedDict()
        self.outputs: dict[str, Path] = {}
        self.reports: dict[str, dict[str, Path]] = {}
        self.latest_revisions: dict[str, int] = {}
        self.decision_fingerprints: dict[str, str] = {}
        self.last_analysis_id: str | None = None
        self.warm = False
        self.warm_warning: str | None = None

    def add(self, analysis: Analysis) -> None:
        with self.lock:
            self.analyses[analysis.id] = analysis
            self.analyses.move_to_end(analysis.id)
            self.last_analysis_id = analysis.id
            while len(self.analyses) > self.max_analyses:
                expired_id, _ = self.analyses.popitem(last=False)
                self.outputs.pop(expired_id, None)
                self.reports.pop(expired_id, None)
                self.latest_revisions.pop(expired_id, None)
                self.decision_fingerprints.pop(expired_id, None)

    def get(self, source_id: str, analysis_id: str | None = None) -> Analysis:
        with self.lock:
            key = analysis_id or self.last_analysis_id
            analysis = self.analyses.get(key or "")
        if analysis is None or analysis.source_id != source_id:
            raise RuntimeError("检测记录不存在或已过期，请重新检测")
        return analysis

    def set_output(self, analysis_id: str, output: Path) -> Path | None:
        with self.lock:
            if analysis_id not in self.analyses:
                raise RuntimeError("检测记录已过期，请重新检测")
            previous = self.outputs.get(analysis_id)
            self.outputs[analysis_id] = output
            return previous

    def set_reports(self, analysis_id: str, files: dict[str, Path]) -> None:
        with self.lock:
            if analysis_id not in self.analyses:
                raise RuntimeError("检测记录已过期，请重新检测")
            self.reports[analysis_id] = files

    def accept_revision(self, analysis_id: str, revision: int) -> None:
        """Reject a plan older than one already processed for this analysis."""

        with self.lock:
            if analysis_id not in self.analyses:
                raise RuntimeError("检测记录已过期，请重新检测")
            previous = self.latest_revisions.get(analysis_id, 0)
            if revision < previous:
                raise RuntimeError(
                    f"计划版本已过期：当前请求 revision={revision}，"
                    f"已处理到 revision={previous}"
                )
            self.latest_revisions[analysis_id] = revision

    def allocate_revision(self, analysis_id: str, fingerprint: str) -> int:
        """Allocate the server-side plan revision from the current decision set."""

        with self.lock:
            if analysis_id not in self.analyses:
                raise RuntimeError("检测记录已过期，请重新检测")
            previous = self.latest_revisions.get(analysis_id, 0)
            previous_fingerprint = self.decision_fingerprints.get(analysis_id)
            if previous and previous_fingerprint == fingerprint:
                return previous
            revision = max(previous + 1, 1)
            self.latest_revisions[analysis_id] = revision
            self.decision_fingerprints[analysis_id] = fingerprint
            return revision

    def report(self, kind: str, analysis_id: str | None = None) -> Path:
        with self.lock:
            key = analysis_id or self.last_analysis_id
            files = self.reports.get(key or "")
        if not files or kind not in files:
            raise FileNotFoundError("这份报告还没生成，请先执行一次处置")
        path = files[kind]
        if not path.is_file():
            raise FileNotFoundError("报告文件已被删除，请重新生成")
        return path

    def output(self, analysis_id: str | None = None) -> Path:
        with self.lock:
            key = analysis_id or self.last_analysis_id
            path = self.outputs.get(key or "")
        if path is None or not path.is_file():
            raise FileNotFoundError("这张图的脱敏副本尚未生成，请先生成")
        return path

    def invalidate_results(self, analysis_id: str) -> Path | None:
        """Drop the current output/report before a new plan is processed."""

        with self.lock:
            self.reports.pop(analysis_id, None)
            return self.outputs.pop(analysis_id, None)

    def discard(self, analysis_id: str) -> tuple[Analysis | None, Path | None]:
        with self.lock:
            analysis = self.analyses.pop(analysis_id, None)
            output = self.outputs.pop(analysis_id, None)
            self.reports.pop(analysis_id, None)
            self.latest_revisions.pop(analysis_id, None)
            self.decision_fingerprints.pop(analysis_id, None)
            if self.last_analysis_id == analysis_id:
                self.last_analysis_id = next(reversed(self.analyses), None)
        return analysis, output

    def clear(self) -> None:
        with self.lock:
            self.analyses.clear()
            self.outputs.clear()
            self.reports.clear()
            self.latest_revisions.clear()
            self.decision_fingerprints.clear()
            self.last_analysis_id = None
            self.warm = False
            self.warm_warning = None


SESSION = Session()


# --------------------------------------------------------------------------
# 示例图库与来源解析
# --------------------------------------------------------------------------


def samples() -> list[dict[str, str]]:
    if not SAMPLE_MANIFEST.is_file():
        return []
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    items = []
    for row in data:
        path = SAMPLES_DIR / row["file"]
        if path.is_file():
            items.append({"id": row["id"], "title": row["title"], "hint": row.get("hint", "")})
    return items


def resolve_source(source_id: str) -> Path:
    """``sample:xxx`` / ``upload:xxx`` → 实际文件路径。"""
    kind, _, key = source_id.partition(":")
    if kind == "sample":
        for row in json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8")):
            if row["id"] == key:
                path = SAMPLES_DIR / row["file"]
                if path.is_file():
                    return path
        raise FileNotFoundError(f"示例图不存在：{key}")
    if kind == "upload":
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("上传标识不合法")
        for suffix in (".jpg", ".png", ".webp", ".bmp"):
            path = UPLOADS / f"{key}{suffix}"
            if path.is_file():
                return path
        raise FileNotFoundError("上传文件不存在或已清除")
    raise ValueError("来源格式不合法")


MAX_IMAGE_PIXELS = 50_000_000
UPLOAD_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp"}


def _validate_image_bytes(payload: bytes) -> str:
    """在落盘前验证真实格式与像素规模，返回可信的 Pillow 格式名。"""
    from PIL import Image, UnidentifiedImageError

    if not payload:
        raise ValueError("不能上传空文件")
    try:
        opened = Image.open(BytesIO(payload))
    except UnidentifiedImageError as exc:
        raise ValueError("无法识别图片；支持 JPG / PNG / WebP / BMP") from exc
    except Image.DecompressionBombError as exc:
        raise ValueError("图片像素过大，已拒绝") from exc
    except (OSError, ValueError) as exc:
        raise ValueError("图片损坏或无法解析") from exc

    with opened:
        image_format = opened.format or ""
        if image_format not in UPLOAD_FORMATS:
            raise ValueError("暂不支持该格式；支持 JPG / PNG / WebP / BMP")
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise ValueError("图片超过 5000 万像素，请先缩小后重试")
        if getattr(opened, "n_frames", 1) != 1:
            raise ValueError("不支持多帧或动态图片")
        try:
            opened.verify()
        except (OSError, ValueError) as exc:
            raise ValueError("图片损坏或无法解析") from exc
    return image_format


def save_upload(filename: str, payload: bytes) -> str:
    """按真实格式保存上传副本，文件名和扩展名均不作为可信输入。"""
    image_format = _validate_image_bytes(payload)
    key = uuid.uuid4().hex
    suffix = UPLOAD_FORMATS[image_format]
    (UPLOADS / f"{key}{suffix}").write_bytes(payload)
    (UPLOADS / f"{key}.name").write_text(Path(filename or "upload").name, encoding="utf-8")
    return f"upload:{key}"


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------


def _public_detection(item: dict[str, Any]) -> dict[str, Any]:
    """给前端的精简视图：只要画框和列表需要的字段。

    不能把整个 Detection 丢给前端——``details`` 里虽然有掩码，但保持白名单
    是最省心的做法，免得以后有人往 details 里塞明文。
    """
    details = item.get("details") or {}
    return {
        "id": item["id"],
        "kind": item["kind"],
        "subtype": item["subtype"],
        "risk": item.get("risk"),
        "bbox": item.get("bbox"),
        "masked_preview": details.get("masked_preview"),
        "idcard_check": details.get("idcard_check"),
        "safe_domain": details.get("safe_domain"),
        "safe_ssid": details.get("safe_ssid"),
        "decodable": details.get("decodable"),
    }


def detect(source_id: str) -> dict[str, Any]:
    path = resolve_source(source_id)
    started = time.perf_counter()
    with ingest_image(path, exiftool_path=_EXIFTOOL) as ingested:
        descriptor = dict(ingested.descriptor)
        image_id = descriptor["image_id"]
        with _DETECTOR_LOCK:
            envelope = analyze_a(ingested.image, image_id)
        detections = tuple(dict(d) for d in (*ingested.detections, *envelope.detections))
    elapsed = (time.perf_counter() - started) * 1000

    analysis = Analysis(
        id=uuid.uuid4().hex,
        source_id=source_id,
        path=path,
        descriptor=copy.deepcopy(descriptor),
        detections=copy.deepcopy(detections),
        timings=dict(envelope.timings_ms),
        warnings=tuple(envelope.warnings),
    )
    SESSION.add(analysis)
    SESSION.warm = True

    actionable = [d for d in detections if d["kind"] != "meta"]
    return {
        "analysis_id": analysis.id,
        "source_id": source_id,
        "image_id": image_id,
        "width": descriptor["width"],
        "height": descriptor["height"],
        "image_url": f"/api/source-image?source={source_id}&v={int(time.time() * 1000)}",
        "detections": [_public_detection(d) for d in detections],
        "meta_count": len([d for d in detections if d["kind"] == "meta"]),
        "actionable_count": len(actionable),
        "timings_ms": analysis.timings,
        "total_ms": round(elapsed),
        "warnings": list(analysis.warnings),
    }


# --------------------------------------------------------------------------
# 处置 + 复检 + 导出
# --------------------------------------------------------------------------

METHODS = {"solid", "blur", "mosaic"}


def _clamp_manual_box(box: Any, width: int, height: int) -> tuple[list[int], str]:
    """把前端来的补框校验并钳进图内；非法输入抛 ValueError，让接口返回 400 而非 500。"""
    if not isinstance(box, dict) or "bbox" not in box:
        raise ValueError("补框数据格式不正确")
    raw = box.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("补框坐标必须是 [x, y, w, h] 四个数字")
    try:
        x, y, w, h = (int(round(float(v))) for v in raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("补框坐标必须是有限数字") from None
    box_method = str(box.get("method") or "solid")
    if box_method not in METHODS:
        raise ValueError(f"不支持的遮盖方式：{box_method}")
    if w <= 0 or h <= 0:
        raise ValueError("补框宽高必须大于 0")
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = min(w, width - x)
    h = min(h, height - y)
    if w < 3 or h < 3:
        raise ValueError("补框太小或完全落在图外")
    return [x, y, w, h], box_method


# 遮盖余量：引擎给的框是「字的外接矩形」，边缘常常压着半个笔画；
# 不扩就会在底纹上留下残影，看起来像没盖干净。
_PAD_RATIO = 0.08
_PAD_MIN = 3


def _vertical_overlap(a: list[int], b: list[int]) -> float:
    """两个框在竖直方向的重叠占较矮那个的比例。同一行的字应接近 1。"""
    top = max(a[1], b[1])
    bottom = min(a[1] + a[3], b[1] + b[3])
    return max(0, bottom - top) / max(1, min(a[3], b[3]))


def _horizontal_gap(a: list[int], b: list[int]) -> int:
    """水平间隙；有重叠返回 0。"""
    if a[0] + a[2] < b[0]:
        return b[0] - (a[0] + a[2])
    if b[0] + b[2] < a[0]:
        return a[0] - (b[0] + b[2])
    return 0


def _union(a: list[int], b: list[int]) -> list[int]:
    x1, y1 = min(a[0], b[0]), min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    return [x1, y1, x2 - x1, y2 - y1]


def _prepare_redaction_boxes(
    detections: list[dict[str, Any]], width: int, height: int
) -> list[dict[str, Any]]:
    """在构造计划之前调整检测框：**合并被拆开的同一个值**，再统一外扩几个像素。

    为什么必须合并：引擎偶尔把一个号码读成两块（例如 `139-1234-` 与 `5678`），
    逐个遮盖会在中间留一条缝，缝里正好是没盖住的数字。2026-11-08 在实拍面单上
    实测到这条缝——导出图里能直接看到 `-56` 露在外面。

    合并判据只用两条，都偏保守：
      1. 同 kind/subtype 且 **掩码完全相同** —— 同一个值被读了两次；
      2. 同 kind/subtype 且竖直重叠 ≥50%、水平间隙 ≤ 0.6 倍行高 —— 一个 token 被切开。
    两条都不满足就不动，避免把同一行上两个**不同**的号码粘成一个。

    外扩是为了盖住框边缘的半个笔画；宁可多盖一点，隐私工具不存在"盖太多"。
    """
    boxes = [list(d["bbox"]) for d in detections if d.get("bbox")]
    index_map = [i for i, d in enumerate(detections) if d.get("bbox")]
    parent = list(range(len(index_map)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(len(index_map)):
        di = detections[index_map[i]]
        ci = boxes[i]
        for j in range(i + 1, len(index_map)):
            dj = detections[index_map[j]]
            cj = boxes[j]
            if di["kind"] != dj["kind"] or di["subtype"] != dj["subtype"]:
                continue
            if _vertical_overlap(ci, cj) < 0.5:
                continue
            mask_i = (di.get("details") or {}).get("masked_preview")
            mask_j = (dj.get("details") or {}).get("masked_preview")
            if mask_i and mask_i == mask_j:
                union(i, j)
                continue
            if _horizontal_gap(ci, cj) <= 0.6 * min(ci[3], cj[3]):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(len(index_map)):
        clusters.setdefault(find(i), []).append(i)

    adjusted = [dict(d) for d in detections]
    for members in clusters.values():
        merged = boxes[members[0]]
        for m in members[1:]:
            merged = _union(merged, boxes[m])
        pad_x = max(_PAD_MIN, round(merged[3] * _PAD_RATIO))
        pad_y = max(_PAD_MIN, round(merged[3] * _PAD_RATIO * 0.5))
        x1 = max(0, merged[0] - pad_x)
        y1 = max(0, merged[1] - pad_y)
        x2 = min(width, merged[0] + merged[2] + pad_x)
        y2 = min(height, merged[1] + merged[3] + pad_y)
        final = [x1, y1, max(1, x2 - x1), max(1, y2 - y1)]
        for m in members:
            adjusted[index_map[m]]["bbox"] = final
    return adjusted


def _overlap_ratio(a: list[int], b: list[int]) -> float:
    """交集面积 / 较小者面积。用于判断"这块有没有被计划覆盖"。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2 = min(a[0] + a[2], b[0] + b[2])
    y2 = min(a[1] + a[3], b[1] + b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / max(1, min(a[2] * a[3], b[2] * b[3]))


def _find_uncovered_qr_like(output_path: Path, plan: dict[str, Any]) -> list[list[int]]:
    """在导出图上找「有二维码形状、但计划没盖到」的区域。

    **为什么需要这一步**：屏幕翻拍的二维码常常解不出内容（摩尔纹破坏模块采样），
    于是检测器不会把它报成 Detection —— 导出的图里就留着一个完好的二维码，
    而报告写着"通过"。用户看一眼就知道不对劲，信任直接没了。

    这里只做**形状检测**（``detect`` 不解码），所以能把"我知道那儿有个码，
    但我读不出它是什么"这种情况抓出来，明确告诉用户："这块我没盖，你自己补一下。"

    解不出的码本来就不该被报成"已处理"。宁可承认没盖，也不能假装通过。
    """
    try:
        import cv2
    except Exception:  # noqa: BLE001 - OpenCV 缺失时跳过，不影响主流程
        return []
    image = cv2.imread(str(output_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    detector = cv2.QRCodeDetector()
    try:
        ok, points = detector.detect(image)
    except Exception:  # noqa: BLE001
        return []
    if not ok or points is None:
        return []
    covered = [item["bbox"] for item in plan.get("items", []) if item.get("bbox")]
    found: list[list[int]] = []
    for quad in points.reshape(-1, 4, 2):
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        box = [int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))]
        if box[2] <= 0 or box[3] <= 0:
            continue
        if any(_overlap_ratio(box, c) >= 0.6 for c in covered):
            continue
        found.append(box)
    return found[:3]


def process(
    source_id: str,
    selected_ids: list[str],
    method: str = "solid",
    manual_boxes: list[dict[str, Any]] | None = None,
    analysis_id: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    if method not in METHODS:
        raise ValueError(f"不支持的遮盖方式：{method}")
    if not isinstance(selected_ids, list) or not all(isinstance(x, str) for x in selected_ids):
        raise ValueError("选中项必须是字符串列表")
    if manual_boxes is not None and not isinstance(manual_boxes, list):
        raise ValueError("补框数据必须是列表")
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
    ):
        raise ValueError("revision 必须是大于等于 1 的整数")

    analysis = SESSION.get(source_id, analysis_id)
    if revision is None:
        fingerprint_payload = {
            "selected_ids": sorted(selected_ids),
            "method": method,
            "manual_boxes": manual_boxes or [],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        revision = SESSION.allocate_revision(analysis.id, fingerprint)
    else:
        SESSION.accept_revision(analysis.id, revision)
    stale_output = SESSION.invalidate_results(analysis.id)
    if stale_output is not None:
        _remove_output_dir(stale_output.parent)
    descriptor = copy.deepcopy(analysis.descriptor)
    detections = copy.deepcopy(list(analysis.detections))
    path = analysis.path

    # 构造计划前先合并「同一个值被拆成的两个框」并外扩几个像素。
    # 不这么做的话，两个框之间会留一条缝，缝里就是没盖住的数字（见函数注释）。
    detections = _prepare_redaction_boxes(detections, descriptor["width"], descriptor["height"])

    actionable = {d["id"] for d in detections if d["kind"] != "meta"}
    chosen = set(selected_ids) & actionable

    decisions = [
        {
            "contract_version": "photocheck/v4",
            "record_type": "Decision",
            "finding_id": det["id"],
            # 取消勾选不是「跳过」，而是显式 retain——契约里没有第三种状态
            "action": "redact" if det["id"] in chosen else "retain",
            "revision": revision,
        }
        for det in detections
        if det["kind"] != "meta"
    ]

    manual_regions = []
    for index, box in enumerate(manual_boxes or [], start=1):
        bbox, box_method = _clamp_manual_box(box, descriptor["width"], descriptor["height"])
        manual_regions.append({"id": f"manual-{index:03d}", "bbox": bbox, "method": box_method})

    out_dir = OUTPUTS / f"run-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        with ingest_image(path, exiftool_path=_EXIFTOOL) as ingested:
            plan = build_redaction_plan(
                descriptor,
                detections,
                decisions,
                revision=revision,
                manual_regions=manual_regions,
                method_by_finding={fid: method for fid in chosen},
            )
            result = process_and_export(
                ingested,
                plan,
                output_dir=out_dir,
                detections=detections,
                decisions=decisions,
                exiftool_path=_EXIFTOOL,
                run_full_recheck=True,
            )
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    elapsed = (time.perf_counter() - started) * 1000

    output = Path(result.export.output_path)
    previous_output = SESSION.set_output(analysis.id, output)
    if previous_output is not None and previous_output.parent != output.parent:
        _remove_output_dir(previous_output.parent)
    verification = result.verification or {}
    report = {
        "analysis_id": analysis.id,
        "disposal_state": verification.get("disposal_state"),
        "checks": verification.get("checks", []),
        "residuals": verification.get("residuals", []),
        "full_recheck": result.full_recheck,
        "export": {
            "name": output.name,
            "sha256": result.export.output_sha256,
            "source_sha256": result.export.source_sha256,
            "source_format": result.export.source_format,
            "pixel_mean_abs_diff": round(float(result.export.pixel_mean_abs_diff), 4),
        },
        "plan_items": len(plan.get("items", [])),
        # 第三层评测必须裁真正的 RedactionPlan，而不是重新猜 Detection bbox。
        # 这里只暴露区域身份、坐标和方式，不包含任何原文或 payload。
        "plan_regions": [
            {
                "id": item["id"],
                "finding_id": item["finding_id"],
                "bbox": list(item["bbox"]),
                "method": item["method"],
            }
            for item in plan.get("items", [])
        ],
        "decisions": {"redact": len(chosen), "retain": len(actionable) - len(chosen)},
        "manual_regions": len(manual_regions),
        "plan_revision": plan["revision"],
        "elapsed_ms": round(elapsed),
        "warnings": list((result.full_recheck or {}).get("warnings", [])),
        # 导出图上「有码形但没盖」的区域：检测器解不出的码不会进 Detection，
        # 只靠复检发现不了，必须单独扫一遍（见函数注释）
        "undecoded_qr": _find_uncovered_qr_like(output, plan),
        "output_path": str(output),
        "output_url": f"/api/output-image?analysis={analysis.id}&v={int(time.time() * 1000)}",
        "exiftool": _EXIFTOOL is not None,
    }
    files = _write_reports(out_dir, report, descriptor)
    SESSION.set_reports(analysis.id, files)
    report["reports"] = {
        "json": f"/api/report?kind=json&analysis={analysis.id}",
        "txt": f"/api/report?kind=txt&analysis={analysis.id}",
    }
    return report


def _write_reports(out_dir: Path, report: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, Path]:
    """把这份处置结果写成两份文件，和脱敏图放在同一个目录。

    **为什么要落盘**：截图不能当凭证。用户把图片和报告一起交出去（交给同事、交给客服、
    作为投诉证据），别人才可能核对"擦了什么、验到哪一步"。只存在浏览器里的数字，
    关掉窗口就没了。
    """
    payload = dict(report)
    payload["image"] = {
        "image_id": descriptor.get("image_id"),
        "width": descriptor.get("width"),
        "height": descriptor.get("height"),
        "source_format": descriptor.get("source_format"),
    }
    payload["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    state = {"passed": "本次计划中待处理区域未发现残留", "pending": "部分待确认",
             "failed": "未通过", "not_applicable": "无需处置"}.get(
        str(payload.get("disposal_state")), str(payload.get("disposal_state")))
    lines = [
        "PhotoCheck 处置报告",
        "=" * 40,
        f"生成时间：{payload['generated_at']}",
        f"图片：{payload['image'].get('image_id')}　"
        f"{payload['image'].get('width')}×{payload['image'].get('height')}",
        f"处置状态：{state}",
        "",
        "计划",
        "-" * 40,
        f"  区域 {payload['plan_items']} 个"
        f"（擦 {payload['decisions']['redact']} 项 / 留 {payload['decisions']['retain']} 项）",
        f"  手动补框 {payload['manual_regions']} 个",
        "",
        "逐区域复检",
        "-" * 40,
    ]
    method_label = {"a_ocr_probe": "文字复检", "a_qr_probe": "二维码复检",
                    "a_face_probe": "人脸复检", "solid_black_region": "纯色块复核",
                    "solid_black_region_structural_only": "纯色块结构复核（语义未验证）",
                    "semantic_verifier_pending": "语义待确认"}
    status_label = {"passed": "本区域未发现残留", "pending": "待确认", "failed": "未通过",
                    "not_applicable": "不适用"}
    for check in payload.get("checks", []):
        region = str(check.get("region_id", "")).replace("redact:", "")
        lines.append(
            f"  {region:<20} {method_label.get(check.get('method'), check.get('method'))}"
            f"　{status_label.get(check.get('status'), check.get('status'))}"
        )
    if not payload.get("checks"):
        lines.append("  （无计划区域，仅清除元数据）")

    fr = payload.get("full_recheck") or {}
    lines += [
        "",
        "整图重扫",
        "-" * 40,
        f"  新增检出 {len(fr.get('detections') or [])} 项　"
        f"计划外残留 {fr.get('unplanned_count', '—')} 项",
        f"  残留明细 {len(payload.get('residuals') or [])} 项",
    ]
    if payload.get("undecoded_qr"):
        lines.append(f"  ⚠ 有 {len(payload['undecoded_qr'])} 处二维码形状未被遮盖（内容解不出）")
    lines += [
        "",
        "导出",
        "-" * 40,
        f"  文件：{payload['export']['name']}",
        f"  SHA-256：{payload['export']['sha256']}",
        f"  原图 SHA-256：{payload['export']['source_sha256']}（未修改）",
        f"  改动像素均值：{payload['export']['pixel_mean_abs_diff']}",
        "",
        "说明：报告只陈述本次可验证的范围，不等于「这张图绝对安全」。",
    ]
    txt_path = out_dir / "report.txt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"json": json_path, "txt": txt_path}


def output_image(analysis_id: str | None = None) -> Path:
    return SESSION.output(analysis_id)


def report_file(kind: str, analysis_id: str | None = None) -> Path:
    return SESSION.report(kind, analysis_id)


def reveal_in_explorer(analysis_id: str | None = None) -> str:
    """在资源管理器里定位导出文件。

    浏览器没法打开本地文件夹，但这个程序就跑在本机——直接把窗口弹出来，
    用户不用去猜 work\\outputs 在哪。
    """
    path = SESSION.output(analysis_id)
    if hasattr(os, "startfile"):
        os.startfile(str(path.parent))  # noqa: S606 - 本地桌面功能，路径来自服务端自身
        return str(path.parent)
    return str(path.parent)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _remove_output_dir(directory: Path) -> bool:
    """Remove one superseded run directory after its replacement succeeds."""

    if (
        directory.exists()
        and directory != OUTPUTS
        and _is_within(directory, OUTPUTS)
    ):
        shutil.rmtree(directory, ignore_errors=True)
        return not directory.exists()
    return False


def cleanup_analysis(analysis_id: str) -> dict[str, bool]:
    """清除一次任务的上传副本与导出目录，绝不触碰原图或示例。"""
    analysis, output = SESSION.discard(analysis_id)
    cleared_output = False
    cleared_upload = False
    if output is not None:
        cleared_output = _remove_output_dir(output.parent)
    if analysis is not None and analysis.source_id.startswith("upload:"):
        source = analysis.path
        if _is_within(source, UPLOADS):
            source.unlink(missing_ok=True)
            source.with_suffix(".name").unlink(missing_ok=True)
            cleared_upload = not source.exists()
    return {"cleared": cleared_output or cleared_upload}


def purge_stale_work(max_age_hours: int = 24) -> int:
    """Delete stale local uploads and run directories after the retention window.

    The browser UI needs the source and current output while a tab is open, so
    files cannot be deleted immediately after processing. A bounded local
    retention window prevents raw uploads and exported reports from accumulating
    indefinitely across runs.
    """

    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, int) or max_age_hours < 1:
        raise ValueError("max_age_hours must be a positive integer")
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for base in (UPLOADS, OUTPUTS):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            try:
                if not _is_within(child, base) or child == base:
                    continue
                if child.stat().st_mtime >= cutoff:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# --------------------------------------------------------------------------
# 预热：第一次检测要加载 OCR / 人脸模型，演示时前 5 秒不能是「点了没反应」
# --------------------------------------------------------------------------


def warm_up() -> None:
    SESSION.warm_warning = None
    try:
        from PIL import Image

        # Warm with a representative full-size sample. A tiny blank image loads
        # the models but leaves the first real-size OCR graph build on the first
        # user request, which was observed as a 20+ second stall.
        image_path = SAMPLES_DIR / "demo-01.jpg"
        with Image.open(image_path) as source:
            image = source.convert("RGB")
        with _DETECTOR_LOCK:
            analyze_a(image, "warmup")
        SESSION.warm = True
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响主流程
        SESSION.warm_warning = f"预热失败：{type(exc).__name__}"


def status() -> dict[str, Any]:
    from photocheck.detectors import a_detector_status

    try:
        detectors = dict(a_detector_status())
    except Exception as exc:  # noqa: BLE001
        detectors = {"error": type(exc).__name__}
    return {
        "warm": SESSION.warm,
        "warm_warning": SESSION.warm_warning,
        "exiftool": _EXIFTOOL is not None and Path(_EXIFTOOL).is_file(),
        "detectors": detectors,
        "work": str(WORK),
    }
