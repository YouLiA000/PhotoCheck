"""环境检查：Python 版本、关键依赖、系统级程序、真实能力探针。

「装了」不等于「能用」——pyzbar 要真的解出一个码，ExifTool 要真的报出版本，
否则等到 W8 演示时才发现链路断了。
"""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path

from audit.model import Context, Result, check, fail, ok, partial, skip

# 规格卡 §0.3 工具总表：包名 → (最低, 最高不包含)。包名用 pip 里的真实名字。
EXPECT = {
    "gradio": ("5.0", "6.0"),
    "pillow": ("10.0", "11.0"),
    "numpy": ("1.26", "2.0"),
    # Spec v1.4 section 0.3 lists opencv-python; the project ships opencv-contrib-python
    # (superset of the same cv2 namespace).
    "opencv-contrib-python": ("4.10", "5.0"),
    "pyzbar": ("0.1", "1.0"),
    "piexif": ("1.1", "2.0"),
    "rapidocr-onnxruntime": ("1.4", "2.0"),
}

# 团队统一固定的 ExifTool 版本，与 tools/check_env.py 的 EXPECTED_EXIFTOOL 保持一致。
EXPECTED_EXIFTOOL = "13.61"


def _ver_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split(".")[:3]:
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


@check("EN-01", "环境", "Python 3.11.x", gate=("W4", "W6", "W8"))
def python_version(ctx: Context) -> Result:
    v = sys.version_info
    label = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) == (3, 11):
        return ok(label)
    return fail(label, "规格卡 §0.3 写死 3.11；用项目 .venv，别用系统 py")


@check("EN-02", "环境", "关键依赖版本在规格卡区间内", gate=("W4", "W6", "W8"))
def deps(ctx: Context) -> Result:
    from importlib import metadata

    bad, good = [], 0
    for pkg, (lo, hi) in EXPECT.items():
        try:
            v = metadata.version(pkg)
        except Exception:
            bad.append(f"{pkg}=未安装")
            continue
        tv = _ver_tuple(v)
        if _ver_tuple(lo) <= tv < _ver_tuple(hi):
            good += 1
        else:
            bad.append(f"{pkg}={v}(需 ∈[{lo},{hi}))")
    if bad:
        return partial(f"{good}/{len(EXPECT)} 合规；{'; '.join(bad[:3])}",
                       "改版本会影响生成/读取两侧，改完跑 test_dataset_integrity")
    return ok(f"{good}/{len(EXPECT)} 全部在区间内")


@check("EN-03", "环境", "pyzbar 解码链路真的能解出码", gate=("W6",))
def pyzbar_probe(ctx: Context) -> Result:
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except Exception as exc:
        return fail(f"import 失败：{type(exc).__name__}", "装 zbar DLL / 重装 pyzbar")

    sample = None
    lbl_dir = ctx.dataset_dir / "dev" / "labels"
    for p in sorted(lbl_dir.glob("*.json")):
        try:
            import json
            recs = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if any(r.get("kind") == "qr" for r in (recs if isinstance(recs, list) else [])):
            img = ctx.dataset_dir / "dev" / "images" / (p.stem + ".jpg")
            if img.is_file():
                sample = img
                break
    if sample is None:
        return skip("找不到含二维码的 dev 样本", "先确认 DS-01 达标")
    try:
        with Image.open(sample) as im:
            got = decode(im.convert("L"))
    except Exception as exc:
        return fail(f"解码抛错：{type(exc).__name__}: {exc}", "zbar DLL 缺失或位数不匹配")
    if not got:
        return fail(f"{sample.name} 解不出任何码", "样本真值说可解就必须能解，查生成器")
    return ok(f"{sample.name} 解出 {len(got)} 个码，payload {len(got[0].data)} 字节")


@check("EN-04", "环境", "ExifTool 可用（K-02 独立核验）", gate=("W6",))
def exiftool(ctx: Context) -> Result:
    exe = ctx.root / "tools" / "exiftool" / "exiftool.exe"
    if not exe.is_file():
        exe_candidates = list((ctx.root / "tools").rglob("exiftool*.exe"))
        if not exe_candidates:
            return partial("未找到 exiftool", "规格卡给了降级路径：用 Pillow 读回自查")
        exe = exe_candidates[0]
    try:
        proc = subprocess.run([str(exe), "-ver"], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return fail(f"调用失败：{exc}", "确认免安装目录完整")
    if proc.returncode != 0:
        return fail(f"退出码 {proc.returncode}", "解压不完整")
    ver = proc.stdout.strip()
    if ver != EXPECTED_EXIFTOOL:
        # 不判 fail：版本差异不改变"有没有 GPS"这种判断，不该拦住闸门。
        # 但必须显示出来——K-02/K-09 的复核结论要能互相比对，
        # 同一个 -a -G1 输出在不同版本间可能增删字段，版本不一致就说不清差异来源。
        return partial(
            f"v{ver}",
            f"团队统一 {EXPECTED_EXIFTOOL}；不一致会让复核输出无法逐字段比对",
        )
    return ok(f"v{ver}")


@check("EN-05", "环境", "Tesseract 5.x + chi_sim（R-04 才需要）", gate=())
def tesseract(ctx: Context) -> Result:
    try:
        import pytesseract
        configured = shutil.which("tesseract")
        candidates = [
            configured,
            r"D:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"D:\Tesseract-OCR\tesseract.exe",
        ]
        executable = next(
            (value for value in candidates if value and Path(value).is_file()),
            None,
        )
        if executable is None:
            raise FileNotFoundError("tesseract executable")
        pytesseract.pytesseract.tesseract_cmd = executable
        v = pytesseract.get_tesseract_version()
        langs = pytesseract.get_languages(config="")
        if "chi_sim" not in langs:
            return partial(f"v{v} 已装但缺 chi_sim", "安装时勾选 Chinese Simplified")
        return ok(f"v{v}，含 chi_sim（{executable}）")
    except FileNotFoundError:
        return skip("未安装", "R-04 阶段再装，现在不阻塞")
    except Exception as exc:
        return fail(f"调用失败：{type(exc).__name__}", "检查 PATH、tessdata 目录和 chi_sim")
