# Start QA Report Tool (uvicorn). Safe for Task Scheduler / manual use.
# Usage: .\scripts\start_server.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "backend\data"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "uvicorn.log"

$existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($existing) {
    $msg = "[{0}] port 8000 already in use (PID {1}), skip start." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $existing.OwningProcess
    Add-Content -Path $LogFile -Value $msg
    Write-Host $msg
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "[$stamp] starting uvicorn ..."

$pyCmd = Get-Command py -ErrorAction SilentlyContinue
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    $inner = "py -3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1"
} elseif ($pythonCmd) {
    $inner = "python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1"
} else {
    throw "python / py not found on PATH"
}

$cmd = "$inner >> `"$LogFile`" 2>&1"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd -WorkingDirectory $Root -WindowStyle Hidden
Start-Sleep -Seconds 2

$listen = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listen) {
    Write-Host "OK: listening on :8000 (PID $($listen.OwningProcess)). Log: $LogFile"
} else {
    Write-Host "WARN: port 8000 not listening yet. Check log: $LogFile"
    exit 1
}
