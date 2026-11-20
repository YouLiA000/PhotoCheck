---
id: BC-0012
title: 中文 / 空格 / 括号 / 下划线开头的文件名直接抛 ContractError——照真实手机导出的文件名调用必崩
found_at: 2026-11-07
layer: detection
reason: rule_miss
symptom: crash
kind: text
subtype: pipeline
card: K-01
severity: P0
status: fixed
spec_ref: 规格卡 DoD「异常输入不得中断」；契约 v4 Identifier 规则 `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`
fix_test: test_analyze_survives_a_chinese_file_name
---

## 现象

```python
analyze("照片.png")
# ContractError: ImageDescriptor.image_id must satisfy the v4 Identifier rule
```

`analyze()` 自动用 `Path(path).stem` 当 `image_id`，而这个字符串**原样**进了契约校验。
契约只允许 `[A-Za-z0-9_.:-]` 且必须以字母数字开头，于是：

| 文件名 | 结果 |
|---|---|
| `express_001.png` | 正常 |
| `照片.png` | **ContractError** |
| `IMG 1234.JPG` | **ContractError**（空格） |
| `截图(1).png` | **ContractError**（括号） |
| `_DSC0001.JPG` | **ContractError**（下划线开头） |
| `3月账单.png` | **ContractError** |

## 复现

```python
from PIL import Image
from core.detect import analyze

Image.new("RGB", (64, 64), "white").save("照片.png")
analyze("照片.png")
# ContractError: ImageDescriptor.image_id must satisfy the v4 Identifier rule
```

命令行一行版：

```bash
python -c "from PIL import Image; Image.new('RGB',(64,64),'white').save('照片.png')"
python -c "from core.detect import analyze; analyze('照片.png')"
```

修复后同一段代码返回 `image_id='img__-096763'`，`width=64`。

## 为什么是 P0

这不是边角情况。照片的来源就是手机和微信，导出名天然带中文、空格、括号、下划线，
相机（DSC）与截图工具全都这么命名。**B 传的是用户在界面上选的文件路径**，
所以这条会在第一次真实使用时就触发，而不是在生产环境偶发。

更糟的是它违反的恰恰是最基本的那条 DoD：异常输入不得中断。

之前 182 条测试一条都没抓到，因为所有测试和样例都用 `express_001` 这种规整名字
——**测试数据比真实数据干净**，这是测试设计本身的问题。

## 根因

```python
descriptor = describe(image, image_id or p.stem)   # p.stem 未经任何处理就进契约
```

契约严格校验本身是对的（`image_id` 会出现在报表和记录里，必须可序列化），
错的是"把不受控的文件名当成受控标识"。

## 修复

新增 `sanitize_image_id()`，只在**自动推导**这条路径上生效：

1. 非法字符（中文、空格、括号、emoji）替换为 `_`；
2. 发生过替换就追加 6 位 CRC32 后缀——否则 `照片` 与 `合同` 会塌缩成同一个 `__`；
3. 首字符非法则加 `img` 前缀（`_DSC0001` → `img_DSC0001-...`）；
4. 超过 128 字符时截断，**且截断本身也触发后缀**——否则 `x*300` 与 `x*301` 会截成同一个 id。

显式传入的 `image_id` 参数**保持严格**，非法照样抛 `ContractError`：那是调用方自己的承诺，
静默改写会把对方的 bug 藏起来。

## 验证

```python
sanitize_image_id("照片")       # 'img__-096763'
sanitize_image_id("IMG 1234")  # 'IMG_1234-173879'
sanitize_image_id("_DSC0001")  # 'img_DSC0001-f11c51'
sanitize_image_id("express_001")  # 'express_001'（合法名不改写，避免所有报表 id 变动）
```

`tests/test_image_id.py` 19 条：合法名不改写、真实名合法化、中文名互不塌缩、
超长名保留后缀、端到端跑通中文文件名、显式非法 id 仍抛错。

## 遗留

`analyze()` 对**非图片文件**（伪装成 .png 的文本）仍抛 `UnidentifiedImageError`，
对 HEIC 文件在未装 `pillow_heif` 时同样抛错。这是"输入不是图片"而不是"图片里有坏数据"，
抛错是合理的，但**B 需要知道 analyze 会抛这两个异常**，不能假设它总返回空结果。
见 `reports/A_功能实现状态_2026-11-07.md` 第三节。
