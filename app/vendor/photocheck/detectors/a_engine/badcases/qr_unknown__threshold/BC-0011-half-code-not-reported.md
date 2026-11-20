---
id: BC-0011
title: 被画框裁掉一半的二维码一个都不报——finder_min_count=3 的直接代价
found_at: 2026-11-07
layer: detection
reason: threshold
symptom: silent_drop
kind: qr
subtype: qr_unknown
card: K-05
severity: P2
status: open
spec_ref: 功能规格卡 v1.4 §K-05「解不出也必须标框」
---

## 现象

`core/detect/qr_evidence.py` 要求未解码候选出示 **3 个定位符**才保留。
一个被页面边缘裁掉部分的二维码最多只剩 2 个定位符，于是：

| 码的可见比例 | cv2 是否给候选 | 是否报出 |
|---|---|---|
| 100% | 给 | ✅ |
| 75% / 65% / 55% | **不给** | ❌ |
| 约 60%（`qr_half_001.png`） | 不给 | ❌ |

严格读 K-05「解不出也必须标框」，这是漏检，不是优化。

## 复现

样本已在库里，不必现造：

```bash
C:/venvs/photocheck/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from core.detect import analyze
r = analyze('samples/synthetic/qr_half_001.png', 'qr_half_001')
print(len(r.detections), [(d['kind'], d['subtype']) for d in r.detections])
"
```

输出 `0 []`。真值 `samples/gt/qr_half_001.json` 里登记了一个
`qr_unknown`，`known_gap: BC-0011`，所以 `eval_detect.py` 会把它单列成
"已登记缺口"，既不计入 CI 闸门，也不会从报告里消失。

## 根因

两道门都过不去，且是叠加的：

1. `cv2.QRCodeDetector` 本身对残缺码连候选四边形都不返回——把
   `finder_min_count` 降到 1 或 2 结果完全一样。**所以这次调参救不了它**。
2. 即便某天 cv2 给了候选，2 个定位符也过不了证据门禁。

第 1 条是关键：这条漏检**不是我把门槛设太高造成的**，是 cv2 的检测器在
候选阶段就已经放弃。换成 pyzbar 也一样——残缺码解不出。

## 未修的原因

不是修不动，是**没有依据判断该不该修**，而且修法不在阈值上：

1. C 的 dev + lock 共 15 条 qr 真值里，**没有任何"半张码"**。真实场景中
   快递单折叠、证件边角出画是否常见，我手上没有数据。
2. 即使 C 补了样本，`finder_min_count` 调小也无效（见根因第 1 条）。
   真要修，得自己做窗口扫描式检测（滑窗 + 定位符匹配），那是**另一个检测器**，
   不在 K-05 现有实现范围内，也不该在 W8 前动。
3. 规格卡没定义"半张码算不算检出"。这一条不澄清，写多少代码都是猜。

## 处置

- 记 `open`，同时在 `samples/` 里留一张可复现的样本（`qr_half_001.png`），
  让这个缺口的规模可测量、可复跑。
- `known_gap` 机制：GT 里标 `known_gap: BC-0011`，评测照报漏检，但 CI 闸门
  只卡"未登记的漏检"。**不允许**把这条直接从真值里删掉——那等于用改标注来提分。
- 判定线（与 C 的 D-1 对齐）：
  - C 的 dev/lock 出现半张码真值 → 本条升 P1，需要独立方案，不进 W8；
  - 到 W12 仍无样本 → 转 `wontfix`，在 README「已知限制」写明
    "被画框裁掉一半的码不会报出"，并附本文件编号。
- 在此之前，界面文案不能说"没有发现二维码"，只能说"本次检测未发现二维码"——
  这条与 B 的 K-09 措辞要求是同一个道理。
