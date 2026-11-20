# 二维码探针素材（2026-11-16）

**这不是数据集的一部分。** 本目录由 `tools/gen_qr_probe.py` 生成，用于补「真实拍照二维码」
这一层证据，**不属于 dev、不属于 lock、不参与封存哈希、不得用于调参**。

## 为什么需要它

现有 43 张 dev + 23 张 lock 全部是程序合成图：没有拍摄角度、没有反光、没有摩尔纹、
文字偏清晰。它们能证明「合成条件下检测正常」，但证明不了「拿手机拍一张能认出来」。
用户在 2026-11-15 反馈的「拍的屏幕二维码检测不到」，正是这块空白。

本目录提供两种素材，分别对应两种验证方式：

| 子目录 | 用途 | 怎么用 |
|---|---|---|
| `source/` | 8 张干净二维码 | **投屏或打印后，用手机实拍**；拍出来的照片才是「真实拍摄素材」 |
| `degraded/` | 56 张模拟拍摄退化图 | 不用相机，直接离线跑鲁棒性，看检测在哪一档崩 |

## 打印件

`print/二维码打印页_A4.pdf`：**10 页**（1 页说明 + 8 页单码 + 1 页总表），A4 / 300 dpi / 黑白。
打印时选「实际大小 / 100%」，不要选「适应页面」。

- 单码页：一页一个码，边长约 12.5 cm，页面下方带 30 / 60 / 120 cm 三个勾选框；
- 总表页：8 个码排成 2×4，适合远景或多码同框。

打印页不是把 `contact_sheet.png` 放大——那样模块边缘会被插值成灰边。
`tools/make_qr_printsheet.py` 按整数模块宽度重新栅格化，并已用 pyzbar 复核：
单码页解出 1 个、总表页 8 个全解出。

```bash
python tools/make_qr_printsheet.py
```

## 实测可解率（生成时自动用 pyzbar 核过）

| 条件 | 可解 / 8 | 说明 |
|---|---:|---|
| `clean` | 8 | 源图，全部可解 |
| `small` | 7 | 整码缩到 96px；密集的 vCard 失败 |
| `blur_soft` / `blur_hard` | 8 / 8 | 高斯模糊 2.0 / 4.0 都还能解 |
| `perspective` | 8 | 旋转 12° + 透视，仍可解 |
| `moire_soft` | 8 | 正弦亮度调制 + 轻微模糊，仍可解 |
| **`moire`** | **0** | **密网格 + 模糊：全部解不出** |
| `jpeg` | 5 | quality=35；3 张密集码失败 |

结论一句话：**当前解码链路对失焦和斜拍不敏感，断崖出现在强摩尔纹**——与实拍反馈一致。
这说明修复方向不是「提高清晰度」，而是「拍屏场景要单独处理」（多尺度重试、去网格、或提示用户改拍纸张）。

## 三条红线

1. **不入数据集**：不进 `dataset/dev`、不进 `dataset/lock`。lock 是按字节封存的，任何新增都会让封存失效。
2. **不用于调参**：本目录的图是「检验素材」，不是训练/阈值调整素材。拿它调参就等于污染测试集。
3. **payload 不进 CSV**：`manifest.csv` 只记 id、subtype、条件、尺寸、哈希、是否可解，
   **不记二维码内容**（符合「二维码 payload 不得进日志/CSV」红线）。

## 重新生成

```bash
python tools/gen_qr_probe.py            # 已存在则拒绝
python tools/gen_qr_probe.py --force    # 清空重建
```

生成器自带门禁：**每张源图必须被 pyzbar 解出且内容一致，否则直接报错退出**，不写半套素材。

## 文件清单

- `source/qr-<id>-clean.png`（8 张）
- `degraded/qr-<id>-<condition>.png`（8 × 7 = 56 张）
- `manifest.csv`：每行的 group / payload_id / expected_subtype / condition / 尺寸 / 可解 / sha256
- `contact_sheet.png`：一张可投屏/打印的对照页，按拍摄顺序排列

`payload_id` 与 subtype 的对应见 `tools/gen_qr_probe.py` 的 `PAYLOADS`；
8 类覆盖 `qr_wifi` / `qr_link` / `qr_geo` / `qr_vcard` / `qr_wechat_pay` / `qr_alipay` / `qr_contact` / `qr_unknown`。
