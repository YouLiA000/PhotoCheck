param(
    [string]$Python = "",
    [int]$NetworkTimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Evidence = Join-Path $Root ("docs\evidence\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

if (-not $Python) {
    $Python = $env:PHOTOCHECK_PYTHON
}
if (-not $Python) {
    $Python = "python"
}

$PythonPath = (Get-Command $Python -ErrorAction SilentlyContinue).Source
if (-not $PythonPath) {
    throw "Python executable not found: $Python"
}

if (-not $env:PHOTOCHECK_EXIFTOOL) {
    foreach ($candidate in @(
        (Join-Path $Root "runtime\exiftool\exiftool.exe"),
        (Join-Path $Root "tools\exiftool\exiftool.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $env:PHOTOCHECK_EXIFTOOL = $candidate
            break
        }
    }
}

$Adapters = @(
    Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, LinkSpeed
)
$Routes = @(
    Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Select-Object InterfaceAlias, NextHop, RouteMetric
)
$activeAdapters = @($Adapters | Where-Object { $_.Status -eq "Up" })
$Adapters |
    Format-Table -AutoSize |
    Out-File -LiteralPath (Join-Path $Evidence "physical-adapters.txt") -Encoding UTF8
$Routes |
    Format-Table -AutoSize |
    Out-File -LiteralPath (Join-Path $Evidence "physical-default-routes.txt") -Encoding UTF8
$firewallProfiles = @()
try {
    $firewallProfiles = @(
        Get-NetFirewallProfile -ErrorAction Stop |
            Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction
    )
}
catch {
    # The main network evidence remains valid on systems where the firewall
    # cmdlets are unavailable; the missing profiles are explicit in the JSON.
}
$connected = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $task = $client.ConnectAsync("example.com", 443)
    $connected = $task.Wait($NetworkTimeoutSeconds * 1000) -and $client.Connected
    $client.Dispose()
}
catch {
    $connected = $false
}

$network = [ordered]@{
    captured_at = (Get-Date).ToString("o")
    active_adapter_count = $activeAdapters.Count
    default_route_count = $Routes.Count
    external_connection_succeeded = $connected
    firewall_profiles = $firewallProfiles
    details_in = @("physical-adapters.txt", "physical-default-routes.txt")
}
$network | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $Evidence "network-summary.json") -Encoding UTF8

$auditOutput = & $PythonPath (Join-Path $Root "photocheck\scripts\audit_offline.py") --run-smoke 2>&1
$auditCode = $LASTEXITCODE
$auditOutput | Set-Content -LiteralPath (Join-Path $Evidence "offline-audit.txt") -Encoding UTF8

Write-Output "Evidence directory: $Evidence"
Write-Output "Active adapters: $($activeAdapters.Count)"
Write-Output "External connection succeeded: $connected"
Write-Output "ExifTool: $($env:PHOTOCHECK_EXIFTOOL)"
Write-Output "Offline audit exit: $auditCode"

if ($connected) {
    Write-Error "A physical external connection is still available; disconnect networking and rerun."
    exit 2
}
if ($auditCode -ne 0) {
    exit $auditCode
}
exit 0
