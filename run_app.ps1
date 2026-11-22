# 启动本地应用
# 用法：powershell -ExecutionPolicy Bypass -File .\run_app.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    throw "找不到 .venv，请先运行 setup.ps1"
}

Set-Location $Root
Write-Host "启动 PhotoCheck（仅本机，勿公开端口）..." -ForegroundColor Cyan
& $py app\main.py
