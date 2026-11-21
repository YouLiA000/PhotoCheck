# 一键取证：网卡 / 路由 / 联网测试 / 离线冒烟
# 用法（请先物理断网）：powershell -ExecutionPolicy Bypass -File .\offline_evidence.ps1
#
# 依赖仓库内脚本：scripts\capture_offline_evidence.ps1
# 产物：docs\evidence\<yyyyMMdd-HHmmss>\network-summary.json

param(
    [string]$Python = "",
    [int]$NetworkTimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

if (-not $Python) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $Python = $venv } else { $Python = "python" }
}

Write-Host "取证前请确认：网线已拔 / Wi-Fi 已关 / 无热点共享" -ForegroundColor Yellow
Write-Host "按 Enter 开始，Ctrl+C 取消 ..."
$null = Read-Host

& (Join-Path $Root "scripts\capture_offline_evidence.ps1") -Python $Python -NetworkTimeoutSeconds $NetworkTimeoutSeconds
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "取证完成：docs\evidence\ 下已生成记录。" -ForegroundColor Green
    Write-Host "别忘了补 5 张人工截图（断网状态 / ping 失败 / 防火墙三栏 / 入站 / 出站）。"
} else {
    Write-Host "脚本返回 $code —— 多半是仍能访问外网，请确认已物理断网后重跑。" -ForegroundColor Red
}
exit $code
