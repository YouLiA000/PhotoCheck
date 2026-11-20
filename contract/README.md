# PhotoCheck · 契约 v4

五类记录是 A / B / C 三人共用的接口。**接口不定，三个人写出来的东西接不上**，所以这一层先行。

| 文件 | 作用 |
|---|---|
| `schema/photocheck-v4.schema.json` | 五类记录的严格定义（JSON Schema 2020-12），`additionalProperties: false` |
| `validate_records.py` | 校验器：结构 + 跨记录不变量 + 泄漏红线 |
| `migrate_v3.py` | v3 历史记录 → v4 的受限迁移 |
| `examples/valid_pipeline.json` | 一条完整闭环（1 张图 / 4 个 Finding / 3 条决定 / 1 份计划 / 1 份复检） |
| `tests/test_validate.py` | 39 条用例，纯标准库 |

## 五类记录

| 记录 | 谁产出 | 关键规则 |
|---|---|---|
| `ImageDescriptor` | B（K-01） | `width/height` 是方向矫正后的尺寸；坐标系固定 `top_left_pixels` |
| `Detection` | A（K-03/04/05/06/07） | 只读事实。`bbox` 在 `kind=meta` 时必须为 null；`evidence` 与 `kind` 固定对应 |
| `Decision` | C（K-11 界面） | 只有 `redact` / `retain`；**元数据不接受 Decision** |
| `RedactionPlan` | B（K-08） | 每个 redact 决定对应且只对应一个区域；手动补框 `finding_id=null`；二维码必须 `solid` |
| `VerificationReport` | B（K-09） | 必须绑定 `plan_revision`；`disposal_state` 由 checks 推出来，不能手写 |

## 怎么用

```bash
# 单条 / 数组 / {"records": [...]} 都吃
python contract/validate_records.py record.json
python contract/validate_records.py contract/examples/valid_pipeline.json --json

# 作为库
from contract.validate_records import validate_records
report = validate_records(records)
report.raise_if_invalid()      # 不通过时抛 ContractError，里面带完整报告
```

装了 `jsonschema` 就走标准实现；没装就走本文件内置的等价实现（覆盖这份 schema 用到的全部关键字，**遇到不认识的关键字直接报错，不会静默放过**）。两条路径的测试都要求全绿。

## 校验分三层

**第一层 · 结构**：类型、枚举、必填、未知字段、`bbox` 尺寸、`detector_score` ∈ [0,1]、`masked_preview` 必须留 `*`。

**第二层 · 跨记录**（JSON Schema 表达不了的）：

| 错误码 | 含义 |
|---|---|
| `duplicate_image_descriptor` / `duplicate_detection_id` | 主键重复 |
| `ambiguous_finding_id` | 同一 `finding_id` 出现在多张图里，而 `Decision` 只带 `finding_id`，无法消歧 |
| `bbox_out_of_bounds` | 框超出工作图 |
| `decision_orphan` | 决定找不到对应 Detection |
| `decision_on_metadata` | 对元数据下决定（元数据整图自动清除） |
| `duplicate_decision_revision` | 同一 finding 的 revision 重复 |
| `redact_without_plan_item` | 决定要遮，但计划里没这块 |
| `plan_item_without_redact_decision` | 计划里有这块，但决定是 retain 或没有决定 |
| `qr_method_not_solid` | 二维码用了 blur / mosaic —— QR 有纠错，半遮可能仍可解 |
| `stale_verification` | `plan_revision` 与当前计划不一致，结论已失效 |
| `check_region_orphan` | 复检区域不在当前计划内 |
| `disposal_state_mismatch` | 四态与 checks 推导结果不符 |
| `residual_retained_orphan` | `reason=retained` 但没有对应的 retain 决定 |
| `failed_check_without_residual` | 区域 failed 却没写 residuals |

**第三层 · 泄漏红线**：递归扫描 `text_ref` / `payload_ref` / `raw_exif` / `payload` / `raw_*` / `thumbnail_*` 等字段名；手机号、身份证号的掩码预览里出现完整号码也判错。

## 改契约的规矩

1. 五类记录的字段增删**必须三人同意**，改的是 `schema/` 这份，不是各自的代码。
2. 加字段要同时补：schema、`tests/` 里一条正例一条反例、`docs/功能规格卡_v1.4.md` §0.2 的说明。
3. 跨记录规则写在 `validate_cross_record()` 里，每条都有独立的错误码，方便脚本判定。

## 两个还没定的口子

- **二维码 subtype 是否继续收缩**：属枚举变更，需三人同意（规格卡 §5 第 7 条同款）。
- **`Decision` 里没有 `image_id`**：规格卡 §0.2 规定 `Detection.id` 只在单个 `image_id` 内唯一。
  单图处理时没问题；批量数据里同一 id 落到两张图，决定就无法消歧。校验器按 warning 提示
  （不是错误——规格卡允许这种命名）。建议约定 id 里带 image_id，例如 `det-<image>-<kind>-<n>`。
