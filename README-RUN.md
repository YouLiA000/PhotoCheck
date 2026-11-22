# PhotoCheck 验收包使用说明

> 版本：2026-11-21（新仓库 + 兼容性/脱敏/评测加固）
> 用途：开发和验收人员的源码环境与 K-14 取证。普通用户应使用
> `PhotoCheck_App_win64_*.zip`，完整解压后双击 `Start.bat`，不需要 Python。

---

## 包内是什么

| 路径 | 内容 |
|---|---|
| `photocheck/` `contract/` `ui/` `eval/` `tools/` `audit/` `app/` `dataset/` | 完整源码与数据集（dev / lock） |
| `tools/exiftool/` | ExifTool 13.61（仓库不含，已预置） |
| `setup.ps1` | 建虚拟环境并安装依赖 |
| `run_app.ps1` | 启动本地应用 |
| `offline_evidence.ps1` | 一键取证：网卡/路由/联网测试/离线冒烟 |
| `docs/` | 规格卡、K-14 清单、取证步骤 |

**不含**：真实授权照片（`dataset/real/`）、本地评测结果（`results/`）、旧 Git 历史。

---

## 第一步：装环境（需要联网，只做一次）

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

脚本会：用 Python 3.11 建 `.venv` → 安装 `requirements-dev.txt`
（包含运行依赖与团队内部 Gradio 原型依赖）→ 跑 `tools/check_env.py`。

**成功判据**：末尾显示「必需项全部就绪」，退出码 0。

> 若本机没有 Python 3.11，先装 3.11（3.13 缺 wheel）。
> 若验收机器完全无网，先在有网机器跑完本步，再把整个文件夹拷过去。

---

## 第二步：自测（需要联网，装完立刻做）

```powershell
.venv\Scripts\python.exe -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests photocheck\contract\tests app\tests -q
.venv\Scripts\python.exe -m pytest photocheck\tests photocheck\tests_integration -q
.venv\Scripts\python.exe -m pytest photocheck\detectors\a_engine\tests -q
```

期望：按仓库当前 `AGENTS.md` 四组命令运行，合计 **766 passed**。
`test_real_photos_local_only` 在非 Git 工作树会显式 skip，不能把该 skip
记成真实照片红线已执行。

---

## 第三步：物理断网取证

按 `docs/K14验收机器断网取证清单_2026-11-06.md` 执行，或直接一键：

```powershell
powershell -ExecutionPolicy Bypass -File .\offline_evidence.ps1 -Python .venv\Scripts\python.exe
```

脚本会在 `docs/evidence/<时间戳>/` 生成 `network-summary.json`，
**若仍能访问外网则返回非零退出码**。

人工还要补 5 张截图（断网状态、ping 失败、防火墙三栏、入站规则、出站规则）——见清单。
