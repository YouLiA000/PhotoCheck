# 建虚拟环境并安装依赖（验收包专用）
# 用法：powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Find-Python311 {
    param([object[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if (-not $candidate.Executable) { continue }
        try {
            $ver = & $candidate.Executable @($candidate.PrefixArgs) `
                -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($ver -eq "3.11") { return $candidate }
        } catch { }
    }
    return $null
}

Write-Host "[1/4] 查找 Python 3.11 ..." -ForegroundColor Cyan
$candidates = @(
    @{ Executable = $env:PHOTOCHECK_PYTHON; PrefixArgs = @() },
    @{ Executable = "python"; PrefixArgs = @() },
    @{ Executable = "py"; PrefixArgs = @("-3.11") },
    @{ Executable = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"; PrefixArgs = @() },
    @{ Executable = "C:\Python311\python.exe"; PrefixArgs = @() },
    @{ Executable = "C:\Python311\Python311\python.exe"; PrefixArgs = @() }
)
$python = Find-Python311 $candidates
if (-not $python) {
    throw "未找到 Python 3.11。请先安装 3.11（3.13 缺 wheel），或设置 PHOTOCHECK_PYTHON 指向 python.exe"
}
Write-Host "      使用：$($python.Executable) $($python.PrefixArgs -join ' ')"

Write-Host "[2/4] 创建 .venv ..." -ForegroundColor Cyan
if (Test-Path ".venv") { Write-Host "      .venv 已存在，跳过创建" }
else { & $python.Executable @($python.PrefixArgs) -m venv .venv }

$vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw "虚拟环境创建失败：$vpy 不存在" }

Write-Host "[3/4] 安装运行与开发依赖 ..." -ForegroundColor Cyan
& $vpy -m pip install --upgrade pip
& $vpy -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败（pip 退出码 $LASTEXITCODE）" }

Write-Host "[4/4] 环境自检 ..." -ForegroundColor Cyan
& $vpy tools\check_env.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "环境自检未通过，按上面的 FAIL 提示处理后再跑一次。" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "完成。下一步：" -ForegroundColor Green
Write-Host "  .venv\Scripts\python.exe -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests photocheck\contract\tests app\tests -q"
Write-Host "  .venv\Scripts\python.exe app\main.py"
