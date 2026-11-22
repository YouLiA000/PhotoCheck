# PhotoCheck Project Instructions

## Project

PhotoCheck is a local, offline photo-privacy inspection and redaction
prototype. The repository combines:

- `photocheck/`: the integrated A detector adapter and B ingest, redaction,
  export, verification, and full-file recheck runtime;
- `app/`: the local browser application and its vendored runtime snapshot;
- `contract/`: the C-side shared v4 contract, validation, masking, risk, and
  migration tools;
- `dataset/`: C's dev, sealed lock, and local-only authorised real samples;
- `ui/` and `eval/`: Gradio UI and K-13 evaluation tooling.

## 铁律（Non-negotiable boundaries）

1. The functional specification is `docs/功能规格卡_v1.4.md`. Never omit it from
   a release archive.
2. Do not put raw OCR text, QR payloads, full phone/ID numbers, or raw EXIF
   values into serialisable records, logs, reports, CSV files, or UI state.
3. Metadata is always removed for the whole image. It never receives a user
   Decision.
4. QR regions are always solid redactions.
5. `passed` means only that no residual was found in the current plan by the
   available probes. Do not claim an image is safe to share.
6. The lock set is sealed by byte hash. Run it only for an approved W8, W20, or
   pre-submission run, and never tune against it.
7. Use Python 3.11 and the team lock files. Do not install both
   `opencv-python` and `opencv-contrib-python`; only the contrib 4.10.0.84 wheel
   is allowed.

## 目录地图

| Path | Purpose |
|---|---|
| `photocheck/` | A adapter and B processing/verification runtime |
| `app/` | Packaged local browser application and runtime snapshot |
| `dataset/` | dev, sealed lock, and local authorised samples |
| `contract/` | C-side v4 validation, masking, risk, and migration tools |
| `ui/`, `eval/` | Gradio interface and K-13 evaluation |
| `audit/` | 进度与合规审计：`python audit/run.py`（`--json-stdout` 给脚本用，`--md` 重生成 `docs/AUDIT.md`） |
| `reports/`, `results/` | 评测与三层判读的生成物（`results/` 入库、`reports/` 归档，均不作运行输入） |
| `work/` | 应用运行期的上传副本与导出目录（24h 清理，**不入库**） |
| `docs/` | specification, handoff, audits, and decisions |

## 提交前

```powershell
python -m pip install pre-commit
pre-commit install
```

已挂一条门禁：`app/vendor/photocheck` 必须与 `photocheck/` 字节级一致
（成品跑 vendor、pytest 跑根包，漂移时测试全绿而成品缺东西）。手动检查：

```powershell
python scripts/sync_app_vendor.py --check
python scripts/sync_app_vendor.py      # 不同步时刷新快照，再把结果一起提交
```

## 坑

- Keep `dataset/lock/**` byte-stable; any byte or line-ending change invalidates
  the seal.
- Do not install `opencv-python` after `opencv-contrib-python`; uninstalling the
  former can delete files shared by both wheels.
- The Windows console may be GBK. Command-line output must remain ASCII-safe.
- A copied runtime under `app/vendor` can drift. Run
  `python scripts/sync_app_vendor.py --check`.

## Runtime

```text
D:\Program Files\PhotoCheck\venv\Scripts\python.exe
```

Dev dependencies (pytest 8.3.3) are pinned in `requirements-dev.txt`; install
them into the shared team venv shown above when a fresh environment lacks
pytest.

Run the web application:

```powershell
python app\main.py
```

Run the major test groups:

```powershell
python -m pytest photocheck\tests photocheck\contract\tests photocheck\tests_integration -q
python -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests -q
python -m pytest app\tests -q
```

`app/vendor/photocheck` is a deployment snapshot of `photocheck`. Check or
refresh it after changing the runtime:

```powershell
python scripts\sync_app_vendor.py --check
python scripts\sync_app_vendor.py
```

## 去哪找

- Current status: `docs/HANDOFF.md`
- Functional requirements: `docs/功能规格卡_v1.4.md`
- Contract details: `contract/README.md` and `photocheck/contract/README.md`
- C delivery audit: `docs/C全量包接入与43图联调_2026-11-08.md`
- Runtime interface manifest: `photocheck/interfaces.json`
