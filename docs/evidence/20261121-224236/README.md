# K-14 验收机离线证据（2026-11-21）

本目录是验收机 `20261121-retest` 的脱敏归档，用于证明 PhotoCheck 在没有外部
网络时仍可完成检测、处置、导出和复检，并记录验收时使用的防火墙出站阻断规则。

## 场景与结果

| 场景 | 证据 | 结果 |
|---|---|---|
| 对照：普通网络可用 | `01-普通网络可用.png`、`network-control.txt` | 外部 HTTPS 可达，证明测试机并非长期断网 |
| 防火墙应用级阻断 | `02-firewall-rules.png`、`firewall-rules-summary.json`、`python-outbound-blocked.txt` | Python 与 ExifTool 出站规则启用、范围为 `Any`、动作为 `Block`；Python 外连返回 `WinError 10013` |
| 物理断网 | `06-外连和DNS均失败.png`、`physical-adapters.txt`、`network-summary.json` | 活动网卡数为 0，外部连接失败，域名解析失败 |
| 离线冒烟 | `offline-audit.txt` | 静态扫描无外连调用，运行时 `disposal_state=passed` |
| 端到端处置 | `physical-final-report.*`、`08-物理断网最终成功.png` | 3 个计划区域、3 项复检、0 残留、0 计划外区域 |

## 归档说明

- 原始采集时间约为 2026-11-21 22:33 至 23:20（Asia/Shanghai）。
- `network-summary.json` 保留了原始采集字段，仅将默认路由中的内网 IP 替换为
  `<redacted-private-ip>`。
- 截图中已不可逆遮盖本机账户、仓库绝对路径、内网 IP 和适配器型号；
  JSON/TXT 摘要中的适配器型号也替换为通用标签，仅保留连接状态。
- 原始 PowerShell 会话、4K 视频和未脱敏截图保留在仓库外，以控制仓库体积并
  避免公开主机细节；原始文件 SHA-256 记录在 `SOURCE_HASHES.json`。
- 本次采集摘要没有保存三个防火墙配置文件各自的 `Enabled` 值，因此审计不把
  “三个 Profile 均启用”当作已证事实；本目录用更直接的材料证明：验收时
  物理网卡全部断开、外连和 DNS 均失败，PhotoCheck 运行时离线跑通，且
  Python/ExifTool 的 `Any` Profile 出站阻断规则已启用。

## 复核方法

在仓库根目录运行：

```powershell
D:\Program Files\PhotoCheck\venv\Scripts\python.exe -B audit\run.py --json --md
```

预期 `EX-01` 为 PASS；若物理断网、离线冒烟或防火墙阻断证据缺任一项，则必须
保持 PARTIAL，不能仅凭截图标题判通过。
