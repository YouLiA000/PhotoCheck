---
id: BC-0013
title: 同一个 OCR 块里重复出现的一行文字，被当成两个实体各报一次——真实召回被虚高成 100%
found_at: 2026-11-13
layer: detection
reason: rule_miss
symptom: false_positive
kind: text
subtype: phone
card: K-03 / K-04
severity: P1
status: fixed
spec_ref: 规格卡 K-04「同一号码在图上出现多次时，每一处都必须被遮盖」——但同一处被识别两次不是两处
fix_test: test_repeated_text_in_one_block_yields_one_detection
---

## 现象

C 的实拍集 `real-001.jpg`（中通面单）上，收件人号码 `139-1234-5678` 被报了两次，
两个框一左一右覆盖同一行文字：

```
mask=138****0000  bbox=[605, 512, 565, 120]   ← 寄件人，正确
mask=139****5678  bbox=[772, 775, 245, 268]   ← 收件人，第 1 次
mask=139****5678  bbox=[1131, 775, 245, 268]  ← 收件人，第 2 次（同一个号码）
```

`real-004.jpg` 同样。

## 根因

不是重复印刷，是 **OCR 把同一行识别了两遍并拼进同一个 block**：

```
bbox=(319, 775, 1057, 268)  text='<地址>
                                  收件人：<姓名><手机号>收件人：<姓名><手机号>'
```

`find_entities` 在这个 block 里匹配到两个完整的 11 位号码（各有自己的字符区间），
`_slice_box` 按区间切出两个框，于是产生两条 Detection。

K-03 的 `_merge_same_line` 按几何关系合并碎框，但这里的问题是**单块内的文本重复**，
不是跨块的框重复，所以合并逻辑碰不到它。

## 复现

不依赖 OCR，直接构造一个"一行文字出现两次"的块：

```python
from core.detect.entities import blocks_to_detections
from core.detect.ocr import TextBlock

block = TextBlock(
    text="地址：上海市世纪大道100号收件人：张三139-1234-5678收件人：张三139-1234-5678",
    bbox=(0, 0, 1000, 60),
    score=0.99,
)
print(len(blocks_to_detections([block], "img-dup")))
# 修复前: 2
# 修复后: 1
```

真实数据复现（需要 C 的实拍集）：

```bash
python scripts/eval_c_dev.py --dataset <dataset>/real \
  --fix-crt "C:/Program Files/Microsoft Office/root/Client"
# 修复前 phone 13/13 = 100%, over-reported 2
# 修复后 phone 11/13 =  84.6%, over-reported 0
```

要看到重复文本本身，打印 OCR 块即可：

```python
from PIL import Image
from core.detect import ocr
for b in ocr.detect_text_blocks(Image.open("real-001.jpg")):
    if "139" in b.text:
        print(b.bbox, b.text)
# (319, 775, 1057, 268) '地址：…收件人：张三139-1234-5678收件人：张三139-1234-5678'
```

## 为什么危害比"多一个框"大

**它让指标看起来更好。** 去重前，C 的实拍集手机号召回算出来是 13/13 = 100%；
去重后是 11/13 = **84.6%**——正好等于 C 独立测出的数字。

也就是说：漏掉的 2 张被重复计数补平了，**一个缺陷伪装成了性能优势**。
这类 bug 最难发现，因为它不报警、不崩、数字还漂亮。

## 修复

`blocks_to_detections` 里按 block 去重，键是 `(subtype, value)`：

```python
seen_in_block: set[tuple[str, str]] = set()
for ent in find_entities(block.text):
    key = (ent.subtype, ent.value)
    if key in seen_in_block:
        continue
    seen_in_block.add(key)
```

**只在同一个 block 内去重**，跨 block 不去重——同一号码出现在寄件人和收件人两处，
是两处真实存在的位置，两处都必须遮盖。这条边界写进了测试
（`test_same_number_in_two_blocks_is_kept_twice`）。

## 验证

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 单 block 内重复文本 | 2 条 | **1 条** |
| 两个 block 各一处 | 2 条 | 2 条（不变） |
| 实拍集手机号召回 | 100%（虚高） | **84.6%** |
| 实拍集 over-reported | 2 | **0** |
| 全量测试 | 230 | **232 passed** |

## 遗留

根因在 K-03（识别层产生了重复文本），本次只在 K-04（规则层）兜住。
没有继续往 K-03 修的原因：重复文本的来源可能是双尺度合并路径，
改动会牵动耗时与召回，需要单独测量；而规则层去重是零风险的。

**这条要在 K-03 侧继续观察**：如果重复文本还导致"号码被切成两半"
（每半都不足 11 位），那规则层去重救不了，必须修识别层。
本次实拍集没有出现这种形态，但样本只有 14 张。
