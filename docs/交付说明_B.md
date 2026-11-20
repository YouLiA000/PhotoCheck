# C 交付给 B：载入 / 处置 / 复检 / 导出对接包

> **交付对象**：B（K-01 载入 / K-02 元数据 / K-08 处置 / K-09 复检 / K-15 集成）
> **用途**：让你拿到可读的数据集与契约、跑通全链路、接 C 的界面输出
> **本包对应的提交**：以包内 `MANIFEST.sha256` 顶部的「提交：」一行为准（本文件不写死哈希，避免过期）。本包含 mediapipe 更正与审计跨仓库改造。

## 一、包里有什么

| 路径 | 用途 | 你改不改 |
|---|---|---|
| `contract/` | 契约 v4 全量：schema / 校验器 / 风险规则 / 掩码 / v3 迁移 | 不改（有意见走 `规格卡修订建议`） |
| `dataset/dev/` | 43 张合成图 + 38 份真值 + CSV + 负例 + manifest | 不改 |
| `dataset/lock/` | **23 张锁定集** + `SHA256SUMS.txt` + `SEALED.json` | **绝对不改** |
| `eval/` | 评测器，二三层评测的入口 | 不改 |
| `ui/app.py` | 界面三页 Tab（检测 / 处置 / 结论） | 不改，但**要接你的处置器** |
| `ui/decision.py` | **Decision 生成 + 手动补框 + 交接包**——这是 C 交给你的东西 | 不改 |
| `ui/datasource.py` | 界面数据从哪来（换真检测器只改这一个文件） | 不改 |
| `docs/` | 8 份文档，见下 | — |

**没给你的**：

| 缺席的 | 为什么 |
|---|---|
| `tools/exiftool/`（30 MB） | 你已有 13.61，不必重复传 |
| `results/` | C 的历史归档 |
| `audit/` | C 的内部检查器 |
| `docs/立项申请书`、`申请书历史/` | 团队内部材料，不在技术对接范围 |

## 二、你批评的三条，逐条回

### 2.1 「C 声称已修正的接口文档仍是旧内容」—— 你看的是旧版

你列的三条我逐条核了当前文件（`docs/AB接口与数据需求_2026-11-07.md`）：

| 你说的问题 | 文件里现在的实际内容 | 行 |
|---|---|---|
| `face: face` | 「`face` 固定为 **`face_region`**（不是 `face`）」 | L60 |
| `full_file_recheck` 层级 | 「`full_file_recheck{}` 是 `ProcessingResult` 的独立字段——不塞进 v4 `VerificationReport`」 | L117 |
| `detector_score` 空值规则 | 「规则类不产生概率，但要**保留上游 OCR 的置信度**」 | L62 |

**三条都改了**，时间在你生成答复之前。你看到的是旧版。

**但你的要求是对的**：「不能只在同步说明里写已改」——我这次把改后的文件**直接放进包里**，你打开就是新的。

### 2.2 「只报规模不够，要可读的数据包」—— 这个包就是

`dataset/` 完整包含：

```
dataset/dev/
  images/image-dev-001.jpg … image-dev-043.jpg    43 张
  labels/image-dev-001.json … image-dev-038.json  38 份真值（契约 v4）
  labels.csv                                       99 行（含 6 条 clean 负样本）
  negatives.json                                   9 条负例区域
  manifest.csv                                     样本清单
dataset/lock/
  images/ labels/ labels.csv manifest.csv negatives.json
  SHA256SUMS.txt     ← 49 个文件的 SHA-256
  SEALED.json        ← 封存时间、文件数、sums 自身哈希
```

**你要求的六条，逐条对照**：

| 你的要求 | C 的实际状态 |
|---|---|
| 每张图有稳定唯一的 `image_id` | ✅ 格式 `image-{dev,lock}-NNN`，与文件名同源 |
| 标注字段符合 `photocheck/v4` | ✅ 每份标签都过 `validate_records`（38/38 通过） |
| 不含原始 OCR 文字 / payload / EXIF 明文 | ✅ 真值里只有 `masked_preview`（掩码）、`safe_domain`/`safe_ssid`；`details` 白名单由 schema 强制 |
| 同场景同模板不跨 dev/lock | ✅ lock 只用发票/工牌/通知书三种模板，dev 零交集 |
| lock 封存后不再调参 | ✅ `seal_lock.py verify` 守护；11-06 重封后未再动图字节 |
| 来源虚构或已授权 | ✅ **全部程序合成**，无真实照片 |

**验封存**：

```bash
python dataset/tools/seal_lock.py verify
# lock 集完好：49 个文件，与封存记录一致
```

### 2.3 「lock 重封后发新哈希」—— 计划在 W8 前，不是现在

你说的 `image-lock-014` 标注修正（L-1 方案 A：图不动、改标注重封）**三方已同意**，但执行时间在 **W8 跑 lock 之前**，现在不做。

**原因**：现在改要重封一次，W8 前还会因为别的原因再封一次——封两次容易出错。一次做完。

**现在的 lock 是完好的**（49 文件，verify 通过）。你要用的话，用**当前这份**；W8 前我改完会发新哈希，并明确标注「旧哈希作废」。

## 三、数据结构：你可能要看的几个细节

### 3.1 真值里的 `details` 有什么

| kind | `details` 允许的字段 | 说明 |
|---|---|---|
| `text` | `masked_preview` / `idcard_check` | `masked_preview` 形如 `138****0000`；`idcard_check` 是 `pass`/`fail`/`na` |
| `qr` | `decodable` / `safe_domain` / `safe_ssid` | `safe_domain` 只给 `qr_link`，`safe_ssid` 只给 `qr_wifi` |
| `meta` | `masked_preview`（可选） | 元数据不给明文 |
| `face` | 无 | subtype 固定 `face_region` |

**schema 的 `additionalProperties: false`** 会拒掉任何多余字段——包括旧别名 `masked_summary` / `domain` / `ssid`（已迁走，W7 前删）。

### 3.2 干净负样本怎么表示

`labels.csv` 里是 `verdict=clean` 的**单独一行**，它没有 Detection。对应的 `labels/*.json` 里只有 `ImageDescriptor`。

**别把这些行删掉**——它们是误报率的分母。删了那几张图就从真值里消失，误报率会虚低。

### 3.3 界面怎么接你的处置器

C 的 `ui/decision.py` 产出两样东西，都装在 `export_payload()` 里：

```python
{
  "contract_version": "photocheck/v4",
  "image_id": "image-dev-001",
  "decisions": [ {contract_version, record_type, finding_id, action, revision}, ... ],
  "manual_boxes": [ {"id": "manual-001", "bbox": [x,y,w,h], "method": "solid"}, ... ],
  "manual_boxes_revision": "sha256:1a2b3c4d5e6f",
  "note": "metadata 始终整图清除，不含 Decision；manual_boxes 仅供构造 RedactionPlan"
}
```

**注意**：`decisions` 是**合法的契约记录**（能直接过 `validate_records`）；`manual_boxes` **不是**契约记录，是给你构造 `finding_id=null` 计划区域的素材。两者分开摆，是怕你误以为补框也是 Detection。

**约束**（也是 `build_redaction_plan` 的硬校验）：

```python
if decided_ids != actionable_ids:
    raise ContractError("every non-metadata Detection requires exactly one Decision")
```

C 保证：**每个非 meta Detection 恰好一条 Decision，meta 一条都不产**。
审计 `RD-01` 每次都在验这件事（实测输出：`2 条 Decision 合契约，meta 已排除，交接包结构完整`）。

### 3.4 `run_full_recheck=True` 必须显式传（这条会决定报告真假）

`process_and_export(..., run_full_recheck=True)` 的默认值是 **False**。漏传的后果：

```
逐区域复检 passed  →  框外的残留没人看  →  报告写 "本次计划中待处理区域未发现残留"
                                          →  用户以为整张图安全，带着残留导出
```

**逐区域复检只探计划遮盖区域，框外残留完全看不见。** 曾经就有一张带框外误报的图被判 `passed`。

C 的调用链一律显式写 `run_full_recheck=True`：

```python
result = process_and_export(
    ingested, plan,
    output_dir=output_dir,
    detections=detections,
    decisions=decisions,
    run_full_recheck=True,        # ← 不许省
)
```

**给 B 的建议**：正式业务入口改成**强制整图重检**——参数不给默认值（逼调用方决定），
或干脆内置、不提供关掉的开关。

理由不是"防御性编程"，是**漏传的后果不对称**：多跑一次整图重检只是慢几秒；
漏传一次，用户拿到的是"报告说通过、实际有残留"的文件。

### 3.5 C 的审计已能按你的 manifest 跨仓库验入口

你给的 manifest 格式我实现了。设 `PHOTOCHECK_REPO_ROOT` 指向你的仓库根：

```bash
PHOTOCHECK_REPO_ROOT=/path/to/_github_photo_check python audit/run.py
```

实测输出：

```
RD-01  PASS | ...跨仓库：photocheck.core.redaction.apply_redactions 可调用
DET-01 PASS | ...跨仓库：photocheck.detectors.a_adapter.analyze_a 可调用
```

**你说的那点是对的**：不能 `getattr(importlib.import_module("photocheck"), name)`（包级没重导出），
必须按 `entries.<name>.module` + `.function` 解析。我已按你的格式改了。

### 3.6 `manual_boxes_revision`：补框的版次（新加，你 11-07 要的那条）

你在核实里指出：补框增减或改坐标会改变 `RedactionPlan`，但 `Decision.revision` 看不出来。
对。`revision` 只跟踪勾选变化，补框不经过 Decision。

现在交接包多一个字段：

```json
"manual_boxes_revision": "sha256:1a2b3c4d5e6f"
```

**它是内容指纹，不是计数器。** 原因：计数器存在界面会话内存里，程序一重启就归零。
你会看到上一批 `revision=5`、这一批 `revision=1`，误判成「更旧的包」。
指纹没有这个毛病——框一样就是一样，改一个像素就变。

判定方式：

| 情况 | 值 |
|---|---|
| 两次导出、框没动 | **相同** |
| 改任一坐标 / 换 `method` / 增减框 | **不同** |
| 没有补框 | `sha256:4f53cda18c2b`（固定值，不是缺字段） |

两条刻意的设计：

- **`id` 不参与**：删掉一个框再加回来会拿到 `manual-002`，但坐标和手法没变，
  对你的计划区域来说不是新批次。
- **`method` 参与**：同一块区域从 `solid` 改 `mosaic`，坐标没变但遮盖效果变了，
  必须算新批次，否则你的复检报告会对着错误的遮盖方式。

**它不进 schema**：`manual_boxes` 本身就不是契约记录（它是构造计划的素材），
所以加这个字段不需要改已冻结的 v4 schema，也不需要三人表决。

### 3.7 包根的 `requirements.txt`，Gradio 已锁死

上一版是 `gradio>=5.0,<6`（范围写法），你要求确定版本。现在是：

```
gradio==5.50.0
```

这个版本下 `ui/tests` **48 条**全过（含 6 条专门测补框版次的）。
`ui/app.py` 里那句 `show_api` 废弃警告来自 Gradio 6.x 的迁移提示，
但 5.50 的 `launch()` **没有** `footer_links` 参数，现在改会直接 `TypeError`——
所以这条留到升 6.x 时一起处理，不是遗漏。

## 四、`mediapipe`：你说对了，我判错了

我在 11-07 20:00 说「`0.10.14` 会把 numpy 拉到 2.4.6，不能共存」——**错的**。

按你的方法验证：

```bash
pip install "mediapipe==0.10.14" "numpy==1.26.4"
# → Would install jax-0.7.1 jaxlib-0.7.1 mediapipe-0.10.14 ml_dtypes-0.5.4 scipy
#   numpy 不在列表里 —— 不动
```

实际装完：

```
numpy 1.26.4 | mediapipe 0.10.14 | solutions True | cv2 4.10.0 | rapidocr ok
detector_status() → {'ocr': 'ok', 'qr': 'ok', 'face': 'ok', 'qr_backend': 'pyzbar'}
```

**根因是你说的**：单独装 mediapipe 时 pip 把 jax 解析到 0.10.2（要 numpy≥2）；给了 numpy 锁，pip 回溯到 jax 0.7.1。

**C 的 `requirements.txt` 已改回 `mediapipe==0.10.14`**，并写明「必须整包装，不能单独装 mediapipe」。你那边的写法没问题。

## 五、还需要你确认的两件

| # | 事项 | 为什么 |
|---|---|---|
| 1 | **`risk` 的绝对值现在不要核** | 规格卡 §831 vs §910 对收款码矛盾（3 vs 2），等 S-3 裁决。C 现在 `SPEC_SCORE` 取 2、`PROVISIONAL` 里名片取 3，是**如实反映两处原文**，不是笔误 |
| 2 | `face` 现在可用后，K-09d 的 `pending` 分支要不要改 | mediapipe 修好后 `unavailable` 不再含 face。你的 K-09d 逻辑要确认：是「unavailable 才 pending」，还是「没检出就 pending」 |

## 六、数据说明（报告要用的）

- 全部**程序合成**，无真实照片、无版权与隐私风险
- 真值 bbox 在绘制时直接产出，**不是人工标注**
- 已知偏差：**无拍摄角度**、**文字偏清晰**、**不含人脸**
- dev **43 张 = 38 张有效 + 5 张故意坏图**（`image-dev-025/026/027/039/040`）；
  有效图共 **84 个对象**。跑批时 5 张坏图应报 `ImageIngestError`，那是预期，不要算失败
- lock 23 张；lock 与 dev **零模板交集**
- n < 30 的指标只能标"开发参考"

## 七、校验本包

```bash
sha256sum -c MANIFEST.sha256
```

包内每个文件都有哈希。不匹配说明传输中被改过，不要用。

> `sha256sum -c` 会先打印几行 `WARNING: … improperly formatted` —— 那是 MANIFEST 顶部的 `#` 说明行，**无害**。判断标准是看正文每一行是否 `…: OK`，只要没有 `FAILED` 就通过。

**lock 的封存是另一层**：`dataset/lock/SHA256SUMS.txt` 由 `seal_lock.py` 生成，只覆盖 lock 那 49 个文件。两层都要过。
