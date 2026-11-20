"""自检：换到一台新电脑上如果打不开，先跑这个。

它做四件事，每件都能独立判断「是环境问题还是程序问题」：

1. 解释器版本与位数——32 位解释器配 64 位 DLL 是经典翻车点
2. 依赖能否导入——缺库会在这里就报出来，而不是等到点检测
3. 三个检测器的状态——OCR / 二维码 / 人脸，谁不可用一目了然
4. 真跑一张示例——端到端出结果才算通过
"""

from __future__ import annotations

import platform
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, BAD, WARN = "[OK]  ", "[FAIL]", "[WARN]"


def line(tag: str, text: str) -> None:
    print(f"{tag} {text}")


def main() -> int:
    print("=" * 62)
    print("  PhotoCheck 自检")
    print("=" * 62)

    line(OK, f"Python {platform.python_version()} ({platform.architecture()[0]})")
    line(OK, f"系统 {platform.system()} {platform.release()}")
    line(OK, f"程序目录 {ROOT}")

    missing = []
    for name, label in (("numpy", "数值库"), ("cv2", "OpenCV"), ("onnxruntime", "推理引擎"),
                        ("rapidocr_onnxruntime", "OCR"), ("PIL", "图像读写"),
                        ("pyzbar.pyzbar", "二维码/条码解码"),
                        ("jsonschema", "契约校验"), ("piexif", "EXIF 写入")):
        try:
            __import__(name)
            line(OK, f"{label}（{name}）可导入")
        except Exception as exc:  # noqa: BLE001
            missing.append(name)
            line(BAD, f"{label}（{name}）导入失败：{exc}")

    try:
        from app import service
    except Exception:
        traceback.print_exc()
        line(BAD, "应用模块导入失败，见上面的堆栈")
        return 1

    status = service.status()
    degraded = []
    critical = []
    for key, value in (status.get("detectors") or {}).items():
        if key == "qr_backend":
            if value == "pyzbar":
                line(OK, "二维码后端 pyzbar")
            else:
                degraded.append("二维码后端已降级，一维条码不可用")
                line(WARN, f"二维码后端 {value}：一维条码不可用，请检查 MSVCR120.dll")
            continue
        tag = OK if value == "ok" else (WARN if key == "face" else BAD)
        line(tag, f"检测器 {key}：{value}")
        if value != "ok" and key != "face":
            critical.append(f"检测器 {key} 不可用：{value}")

    exiftool = service.exiftool_path()
    if exiftool and Path(exiftool).is_file():
        try:
            version = subprocess.run([str(exiftool), "-ver"], capture_output=True, text=True,
                                     timeout=15).stdout.strip()
        except Exception:  # noqa: BLE001
            version = "版本未知"
        line(OK, f"ExifTool {version}（{exiftool.parent.name}）")
    else:
        line(WARN, "未找到 ExifTool：元数据仍会被清除，但少了独立复核这一道")

    samples = service.samples()
    line(OK if samples else BAD, f"内置示例 {len(samples)} 张")

    if missing:
        print("\n结论：缺少依赖，程序无法运行。")
        return 1

    if critical:
        for issue in critical:
            line(BAD, issue)
        print("\n结论：关键检测器不可用，无法完成隐私体检。")
        return 1

    print("\n跑一张示例图做完整检测、脱敏导出与复检 …")
    analysis_id = None
    try:
        source_id = f"sample:{samples[0]['id']}"
        result = service.detect(source_id)
        analysis_id = result["analysis_id"]
        selected = [
            detection["id"]
            for detection in result["detections"]
            if detection.get("kind") != "meta"
        ]
        processed = service.process(
            source_id,
            selected,
            method="solid",
            analysis_id=analysis_id,
            revision=1,
        )
        output = service.output_image(analysis_id)
        report_json = service.report_file("json", analysis_id)
        if not output.is_file() or not report_json.is_file():
            raise RuntimeError("导出图或复检报告未生成")
        if processed.get("disposal_state") not in {
            "not_applicable",
            "pending",
            "passed",
            "failed",
        }:
            raise RuntimeError("复检报告缺少合法 disposal_state")
        line(
            OK,
            f"端到端通过：检测 {len(result['detections'])} 项，"
            f"处置 {processed.get('plan_items', 0)} 个区域，"
            f"复检状态 {processed.get('disposal_state')}",
        )
    except Exception:
        traceback.print_exc()
        line(BAD, "端到端检测、导出或复检失败")
        return 1
    finally:
        if analysis_id:
            service.cleanup_analysis(analysis_id)

    if degraded:
        for issue in degraded:
            line(WARN, issue)
        print("\n结论：程序可启动，但二维码/条码能力不完整。")
        return 1

    print("\n结论：一切正常，双击「启动.bat」即可使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
