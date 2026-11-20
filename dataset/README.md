# dataset

真值数据。**这是研究的资产，不是演示的素材**——结题时能独立成立的是数据集 + 评测协议 +
有效性报告，不是功能的多少。

## 布局

```
dataset/
├─ dev/                  开发集，随便用
│  ├─ images/            image-dev-001.jpg … 043.jpg（实有 43 张）
│  ├─ labels/            38 份真值，契约 v4 记录（ImageDescriptor + Detection[]）
│  │                      025 / 026 / 027 / 039 / 040 五张尚未标注
│  ├─ negatives.json     「像但不是」的负例框，评测误报率用
│  ├─ labels.csv         标注面（Excel 改这个，导回去再校验）
│  └─ manifest.csv       样本清单：模板、尺寸、质量、EXIF、正负例数
├─ lock/                 锁定集 23 张（发票 / 工牌 / 录取通知书），已封存哈希
│  ├─ SHA256SUMS.txt    封存记录；verify 对不上就说明被改过
│  └─ SEALED.json       封存时间、张数、sums 自身的哈希
└─ tools/
   ├─ gen_samples.py     合成样本生成器（固定 seed，可复现；--set dev|lock）
   ├─ seal_lock.py       lock 集封存 / 校验
   ├─ labels_csv.py      标注 CSV ↔ 真值 双向转换
   └─ tests/
```

## 三条铁律

1. **锁集独立性的量化**：`python dataset/tools/near_duplicate.py`
   算 dev×lock 全部 840 对的感知哈希距离。当前结果：**跨集最近的一对差 12 位、0 对达到
   近重复候选（≤10）**；而 dev 内部最近 0 位（40 对 ≤5，同模板版式一致）。同集比跨集更像，
   说明两套确实不同构——这比"模板名不同"硬得多。
2. **lock 集不参与开发验收**。调阈值、试参数、看效果一律用 dev。lock 只在 W8 / W20 / 答辩前
   各跑一次，跑完就不能再改算法再跑第二次——那等于自己给自己出题又改答案。
   **lock 用独立模板**（发票 / 工牌 / 录取通知书），与 dev 的五个模板零交集；
   `test_lock_templates_disjoint_from_dev` 会在模板被混用时直接报错。
2. **CSV 里不落明文**。`masked_preview` 必须带 `*`；二维码内容一个字都不进表，`note` 也不行。
   标注文件本身泄露原图信息，是最讽刺的一类事故。
3. **改数据必须能解释**。新增样本走生成器加 spec，不手工塞图；手改 CSV 要写 `note` 说明原因。

## 重新生成

```bash
python dataset/tools/gen_samples.py                 # 全量重生成 dev（像素级一致）
python dataset/tools/gen_samples.py --only 3,7      # 只重生成某几张
python dataset/tools/gen_samples.py --out dataset/lock --seed-base 5000   # 换 seed 造 lock 集
python dataset/tools/labels_csv.py export           # 真值 → CSV
python dataset/tools/labels_csv.py import           # CSV → 真值并校验

python dataset/tools/gen_samples.py --set lock      # 生成锁定集（新模板）
python dataset/tools/seal_lock.py seal              # 封存并记哈希
python dataset/tools/seal_lock.py verify            # 校验是否被改动过
```

标注口径见 `docs/标注指南_R-01_v1.md`。
