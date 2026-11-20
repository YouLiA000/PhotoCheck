---
id: BC-0008
title: pyzbar 的 libzbar DLL 加载失败被静默忽略，整个 sprint 实际用 cv2 兜底，一维条码完全不可用
found_at: 2026-11-02
layer: env
reason: ocr_miss
symptom: silent_drop
kind: qr
subtype: pipeline
card: K-05
severity: P1
status: open
spec_ref: 规格卡 K-05「二维码 / 一维条码检测解码」
---

## 现象

`pyzbar` 装了，DLL 也在，但加载失败：

```
FileNotFoundError: Could not find module
'...\pyzbar\libzbar-64.dll' (or one of its dependencies).
```

而 `pyzbar_available()` 是一个裸 `try/except`，失败返回 `False`，
`detect_qr()` 就静默走 cv2 兜底。结果是：

| 能力 | pyzbar | cv2 兜底 |
|---|---|---|
| QR 二维码 | ✅ | ✅ |
| EAN13 / Code128 等一维条码 | ✅ | ❌ **完全没有** |

而 `detector_status()` 一直返回 `{"qr": "ok"}`，README 的依赖表也写着 `pyzbar ✅`。
**一整个 sprint 都以为一维条码是支持的。**

## 复现

```bash
C:/venvs/photocheck/Scripts/python.exe -c "
try:
    from pyzbar import pyzbar; print('pyzbar OK')
except Exception as e:
    print('pyzbar FAIL:', type(e).__name__, e)
from core.detect.qr import backend; print('实际后端:', backend())
"
```

## 根因

两层原因叠加：

1. **环境**：`libzbar-64.dll` 存在且是合法 PE，但依赖加载失败。
   **2026-11-07 定位到具体缺项**：`libzbar-64.dll` 的导入表里写着

   ```
   KERNEL32.dll  USER32.dll  WINMM.dll  MSVCR120.dll  libiconv.dll  libzbar64-0.dll
   ```

   这台机器 `C:\Windows\System32\MSVCR120.dll` **不存在**（64 位 VC++ 2013 运行库没装），
   `SysWOW64` 下那份是 32 位，64 位 Python 用不上。

   验证方式（不改代码、不 uninstall 任何包，只把一份 64 位 CRT 目录加进 DLL 搜索路径）：

   ```python
   import os, ctypes
   os.add_dll_directory(r"C:\Program Files\Microsoft Office\root\Client")  # 那里有 64 位 msvcr120.dll
   ctypes.CDLL(r"...\pyzbar\libzbar-64.dll")   # 原来 OSError，加了之后成功
   ```

   加载成功即证明"缺的是 CRT，不是 DLL 文件、不是位数、不是包坏了"。
   之前"已排除 MSVC 运行时齐全"的结论是误判——查的是 `vcruntime140`（VS2015+），
   而 libzbar 要的是 `MSVCR120`（VS2013），两者不是一回事。
2. **代码**：可用性探测只回答"能不能 import"，不回答"实际用哪个后端"。
   import 失败 → 静默降级 → 没有任何地方能看出能力缩水了。

第 2 点才是真问题：任何依赖都可能在某台机器上部分失效，
**"降级了但没人知道"比"直接报错"危险得多**。

## 处置

已做（本 sprint）：
- 新增 `qr.backend()`，返回 `pyzbar` / `cv2` / `none`；
- `detector_status()` 增加 `qr_backend` 字段，降级对调用方可见；
- README 依赖表如实标注为 `⚠️ 本机 DLL 加载失败，当前走 cv2 兜底`；
- `check_env.py` 增加根因诊断 `_pyzbar_blocker()`：直接打印"缺 64 位 MSVCR120.dll +
  装 VS2013 x64 运行库"，不再只说"pyzbar 不可用"（后者会把人引去重装 wheel，永远无效）。

结论（回答 C 的 A-3）：
- **不是"Python 生态在 Windows 上不支持一维条码"**，也不是 K-05 的范围问题；
  K-05 原文含一维条码，所以这是**功能缺口**，但缺口来自**主机缺 CRT**，不是代码。
- **修复不需要改代码、不需要换依赖**：装一次 VS2013 x64 运行库即可，pyzbar 恢复后
  一维条码自动可用。这与 ExifTool 同类——属于"环境前置条件"，应写进 README。

待办：
- 本机无管理员权限，无法就地装 redist。已把这条写进 README「环境前置条件」，
  并要求 `check_env` 启动时先报（已完成）。
- 装好后再跑一次 `backend()`，若返回 `pyzbar`，用一张真实 EAN-13 补进 `samples/`，
  把 BC-0008 转 `fixed`。
- 备选（不引入新依赖）：contrib 的 `cv2.barcode.BarcodeDetector` 存在且可创建，
  但我用自绘 EAN-13（95 模块、3 种尺度、加模糊/降采样）**全部解不出**，
  因此不能拿它当替代方案宣称"一维码已支持"。要有真实条码样本才能评估。

判定线：K-05 的验收里若含一维条码正样本（规格卡写的是"二维码 / 一维条码"），
这条必须在 W4 前关闭；若数据集里没有条码样本，转 `wontfix` 并写明。

## 追加（2026-11-07 20:45）：功能已在注入运行库后验证通过

B 的环境实测 `qr_backend=pyzbar`，说明代码本身没问题，故障在主机缺运行库。
本机没有管理员权限装 redist，但可以用 `os.add_dll_directory()` 把已有的 64 位
`MSVCR120.dll`（`C://Program Files\Microsoft Office\root\Client`）加进搜索路径，
让 pyzbar 先加载起来做验证：

```bash
python scripts/verify_barcode.py --fix-crt "C:/Program Files/Microsoft Office/root/Client"
```

结果（产物在 `reports/barcode_evidence/`）：

| 用例 | 结果 |
|---|---|
| EAN-13 `6901234567892` 正例 | **1 条 `qr_code` / risk 1 / bbox [120, 70, 380, 119]** |
| 同图、校验位故意错 | 0 条 |
| 装饰竖条纹（无起始/终止符） | 0 条 |

两个负例是关键：只验正例的话，"把每条竖纹都当条码"的实现也能通过。

**结论变化**：A 侧没有代码欠账，`qr_code` 分类与 bbox 都正确。
这条仍记 `open`，但性质从"能力缺口"变成"主机环境待修"——
装一次 VS2013 x64 运行库即消失。`--fix-crt` 只是验证手段，产品代码不做这个注入。
