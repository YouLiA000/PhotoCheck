# PhotoCheck：照片分享前隐私体检与脱敏工具

PhotoCheck 是一个面向校园场景的本地照片隐私检查原型。它在不上传照片、
不调用在线 API 的前提下，检查图片中的手机号、身份证号、二维码、人脸和
敏感元数据，按用户决定生成脱敏副本，并对导出文件做结构复检和内容复检。

> `passed` 只表示当前计划区域和本轮可用检测器没有发现残留，不代表照片
> “绝对安全”或“可以放心公开”。原始文件始终不被修改。

英文说明见 [README.en.md](README.en.md)。

## 主要能力

- 支持 JPG、PNG、WebP、BMP，统一处理 EXIF 方向和像素坐标。
- 检测手机号、身份证号、二维码、人脸及常见拍摄元数据。
- 元数据整图清除，仅保留新生成的标准 sRGB ICC 配置。
- 二维码和人脸区域强制使用不透明实心遮盖。
- 支持用户决定、手动补框、全局 revision 和旧结果失效。
- 导出后执行逐区域复检与整图重扫，输出 JSON/TXT 报告。
- 应用只监听 `127.0.0.1`，并校验 Host、Origin 和 `Sec-Fetch-Site`，
  阻止 DNS rebinding 与普通跨站请求。
- 全程离线运行；K-14 断网、DNS 失败、防火墙阻断和离线冒烟证据已归档。

## 当前状态

- 主分支：`main`
- 当前审计：**42 PASS / 0 PARTIAL / 0 FAIL / 0 SKIP**
- 闸门：W4、W6、W8 均通过
- 数据集：**43 张合成 dev 图**（38 张有真值）、23 张锁定图
- 四组回归：**766 passed**
- 隐私扫描：**542 个文件通过**
- R-04：两位实名评审各完成 30 个对象，结论一致率 100%，Cohen's kappa 1.0
- K-14：验收机物理断网、外连/DNS 失败、防火墙阻断和离线冒烟证据齐全
- 环境：Python 3.11、ExifTool 13.61、Tesseract 5.5.3

当前仍是 `v1.0-demo` 发布准备阶段。发布前还需完成 R-05 候选包审核、
独立空目录复现和 tag。产品演示录像按用户决定延期，不阻塞当前候选包。

## 普通用户：使用便携包

1. 取得 `PhotoCheck_App_win64_*.zip`。
2. 完整解压到可写目录，不要直接在 ZIP 内运行。
3. 双击 `Start.bat`，或中文名 `启动.bat`。
4. 浏览器会打开 `http://127.0.0.1:7861/`。

首次打不开时先运行 `Check.bat` / `自检.bat`。便携包自带 Python 和
ExifTool，不需要另行安装，也不需要联网。

## 源码开发

要求：

- Windows 10/11 64 位
- Python 3.11
- ExifTool 13.61
- Tesseract 5.5.3 + `chi_sim`（仅 R-04 需要）

建立虚拟环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

启动应用：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_app.ps1
```

`run.bat` 是遗留兼容入口；源码环境的正式入口是
`setup.ps1` + `run_app.ps1`。

## 环境检查

```powershell
python tools\check_env.py
python photocheck\scripts\check_environment.py
```

ExifTool 查找顺序统一为：

1. 显式参数；
2. `PHOTOCHECK_EXIFTOOL`；
3. 便携布局 `runtime\exiftool\exiftool.exe`；
4. 开发布局 `tools\exiftool\exiftool.exe`；
5. 系统 `PATH`。

缺少 ExifTool 或版本不是 13.61 时，自检会失败，不再报告“全部就绪”。

## 使用流程

1. 上传图片或选择内置示例。
2. 查看检测结果、风险等级和掩码预览。
3. 对每个发现选择保留或遮盖。
4. 对漏检区域手动补框。
5. 生成脱敏副本。
6. 查看逐区域复检、整图重扫、残留和计划外发现。
7. 下载脱敏图片及 JSON/TXT 报告。

决定发生变化时，服务端会分配新的全局 revision，旧复检结果自动失效。

## 输出与隐私边界

- 原图只读，导出始终写入新文件。
- 原始 OCR 文本、二维码 payload、完整号码和原始 EXIF 值不进入 JSON、
  CSV、日志、UI state 或公共仓库。
- 本地操作副本位于 `work/`，默认 24 小时清理。
- 授权真实照片只保存在本机 `dataset/real/`，Git 默认忽略。
- 锁定集 `dataset/lock/` 由字节哈希封存，只在批准的验收运行中使用。
- Gradio 只属于 `ui/` 内部联调依赖，不进入成品运行依赖。

## 测试

```powershell
python -m pytest photocheck\tests photocheck\contract\tests -q
python -m pytest photocheck\detectors\a_engine\tests photocheck\tests_integration -q
python -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests -q
python -m pytest app\tests -q
```

隐私扫描：

```powershell
python tools\privacy_scan.py
```

提交前同步并检查 vendor：

```powershell
python scripts\sync_app_vendor.py
python scripts\sync_app_vendor.py --check
```

## 进度审计

```powershell
python -B audit\run.py
python -B audit\run.py --json --md
```

审计报告见 [docs/AUDIT.md](docs/AUDIT.md)，当前交接状态见
[docs/HANDOFF.md](docs/HANDOFF.md)。

## 离线取证

先在验收机物理断网，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\offline_evidence.ps1 `
  -Python .venv\Scripts\python.exe
```

证据写入 `docs/evidence/<时间戳>/`。当前已归档证据见
[docs/evidence/20261121-224236/README.md](docs/evidence/20261121-224236/README.md)。

## 构建便携包

```powershell
python tools\build_app.py
```

构建脚本会：

- 先跑应用测试；
- 同步 `app/vendor/photocheck`；
- 复制 Python、依赖和 ExifTool；
- 拒绝缓存、上传副本、运行产物和测试目录；
- 检查功能规格卡是否存在；
- 生成 ZIP 和外部 SHA-256 文件。

发布包必须包含 [功能规格卡 v1.4](docs/功能规格卡_v1.4.md)。

## 目录

| 路径 | 用途 |
|---|---|
| `app/` | 本地浏览器成品及 vendor 运行时 |
| `photocheck/` | 载入、脱敏、导出、复检和 A 检测器适配层 |
| `contract/` | 共享 v4 契约、校验、掩码和迁移工具 |
| `dataset/` | dev、lock 和本机授权样本 |
| `eval/` | K-13 评测 |
| `ui/` | Gradio 内部联调界面 |
| `audit/` | 进度与合规审计 |
| `reports/` | 可提交的评测和基线产物 |
| `results/` | 本地评测、R-04 材料等生成物，默认忽略 |
| `docs/` | 规格卡、决策、验收证据和交接文档 |
| `copy/` | 清理前备份，默认忽略且不打包 |

## 已知边界

- 评测主要基于合成 dev 集，结果不能直接外推到所有实拍场景。
- `passed` 不能证明人眼绝对无法识别，也不替代人工复核。
- K-07 人脸检测仅提供区域提示，不做身份识别。
- 当前不承诺 HEIC、TXT 等格式进入成品处理链。
- 姓名、地址、邮箱等 S-7 扩展类型仍不在当前核心验收范围。

## 文档入口

- [验收与使用说明](README-RUN.md)
- [集成状态](docs/HANDOFF.md)
- [进度审计](docs/AUDIT.md)
- [功能规格卡 v1.4](docs/功能规格卡_v1.4.md)
- [评测协议](docs/评测协议_v1.0.md)
- [迁移与清理记录](docs/交付清单_v1.0.md)
