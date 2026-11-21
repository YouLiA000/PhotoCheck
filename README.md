# PhotoCheck

PhotoCheck is a local photo-privacy inspection prototype for Chinese campus
scenarios. It currently implements the B-side processing infrastructure:

- canonical JPEG/PNG/WebP/BMP ingestion with EXIF orientation normalization;
- sensitive metadata detection through ExifTool;
- metadata-free pixel reconstruction and safe exports;
- solid, blur and mosaic region redaction;
- structural verification and v4 verification reports;
- strict PhotoCheck v4 data-contract validation;
- validation of detector-side A runtime envelopes.

## Current status

This directory is the clean repository created on 2026-11-20. It contains the
integrated A/B/C implementation, the frozen dev/lock datasets, the A engine,
evaluation and audit tooling, and the local browser application.
The dataset contains 43 张合成 dev images (38 labelled), 23 sealed lock images,
and local-only authorised real samples.

The 2026-11-21 regression run is:

- contract/data/UI/eval/tool/application groups: **421 passed**
- B runtime + A/B integration: **69 passed**
- A engine: **238 passed**
- environment preflight: all required dependencies OK

The previous K-07 sample gap was closed locally on 2026-11-20: **22 strict
single-face authorised samples** are prepared and all 22 have metadata removed.
The two multi-person photos remain quarantined and are not counted.

The only known incomplete human-evidence item is the two-reviewer R-04 manual
judgement. A passed state is never interpreted as an absolute safety claim.

Run the local application with:

```powershell
python app\main.py
```

On Windows the repository-root launcher is also available:

```powershell
run.bat
```

Build the portable Windows directory and ZIP:

```powershell
python tools\build_app.py
```

The build refuses `work/`, bytecode caches, missing contract documents, a broken
bundled interpreter, or an app entry that cannot serve `/api/health`.

See [the integration record](docs/A交付整合与下一步_2026-11-02.md) for the
historical delivery audit and [the forward plan](docs/后续推进计划_2026-11-06.md)
for current work.

## Environment

- Python 3.11
- Pillow 10.4.0
- piexif 1.1.3
- jsonschema 4.26.0
- opencv-contrib-python 4.10.0.84
- NumPy 1.26.4
- External ExifTool 13.61
- Tesseract 5.5.3 + `chi_sim` for R-04 cross-checking

ExifTool 13.61 archive:

```text
https://exiftool.org/exiftool-13.61_64.zip
SHA256 = AB75B37515385A753AA60A58F68FFFFE1FF81E43B319DF9540978FB19E0580E5
```

ExifTool is a required external prerequisite for both metadata detection and
export verification. Run the preflight before using PhotoCheck:

```powershell
python photocheck/scripts/check_environment.py
```

Install the full runtime into an isolated Python 3.11 environment:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_env.ps1 `
  -PythonPath C:\Python311\Python311\python.exe `
  -VenvPath "D:\Program Files\PhotoCheck\venv" `
  -InstallDev
```

The bootstrap normalizes OpenCV to `opencv-contrib-python==4.10.0.84` after
dependency resolution and then runs the environment preflight.

ExifTool is an external program. Set `PHOTOCHECK_EXIFTOOL` to its executable.
Portable builds place it at `runtime/exiftool/exiftool.exe`, and the launcher
sets the same environment variable automatically. The development fallback is:

```text
D:\Program Files\PhotoCheck\ExifTool-13.61\exiftool.exe
```

## Tests

Run from the repository root:

```powershell
python -m pytest photocheck\tests photocheck\contract\tests -q
python -m pytest photocheck\detectors\a_engine\tests photocheck\tests_integration -q
python -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests -q
python -m pytest app\tests -q
```

Audit a newly delivered A package:

```powershell
python photocheck/scripts/check_a_delivery.py <A-delivery-directory>
```

Run the integrated A smoke test:

```powershell
python photocheck/scripts/smoke_a_engine.py photocheck/detectors/a_engine/express_001.png
```

Build a clean release archive. The script excludes `.git`, caches and local
outputs, and fails unless `docs/功能规格卡_v1.4.md` is present:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_release.ps1
```

## Documentation

- [Functional specification v1.4](docs/功能规格卡_v1.4.md)
- [System flow v3](docs/PhotoCheck系统流程图_v3.html)
- [Integrated status and next steps](docs/HANDOFF.md)
- [External review verification and fixes, 2026-11-14](docs/外部审查核实与修复_2026-11-14.md)
- [C full-package audit and 43-image run](docs/C全量包接入与43图联调_2026-11-08.md)
- [A integration status and next steps](docs/A交付整合与下一步_2026-11-02.md)
- [Forward implementation plan 2026-11-06](docs/后续推进计划_2026-11-06.md)
- [K-14 offline verification record](docs/K14离线验证记录_2026-11-06.md)
- [K-14 acceptance-machine evidence steps](docs/K14验收机器断网取证清单_2026-11-06.md)
- [A synchronization checklist](docs/A同步修复清单_2026-11-06.md)
- [C integration checklist](docs/C最小界面对接清单_2026-11-06.md)
- [C task verification and B votes](docs/C任务核实与三人表决_B意见_2026-11-07.md)
- [B integration package for A](docs/发给A_B集成测试包清单_2026-11-06.md)
- [Package usage details](photocheck/README.md)

## Privacy boundary

PhotoCheck removes all source metadata before export and embeds only a newly
generated standard sRGB ICC profile. It does not claim that an exported image is
absolutely safe or that a same-detector recheck proves human unreadability.

