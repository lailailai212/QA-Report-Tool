# Register Windows Scheduled Task to auto-start QA Report Tool.
# Default: AtLogOn (usually no admin). -AtStartup requires elevated PowerShell.
param(
    [string]$TaskName = "QA-Report-Tool",
    [switch]$AtStartup
)

$ErrorActionPreference = "Stop"

function Test-IsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$Root = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start_server.ps1"
if (-not (Test-Path $StartScript)) {
    throw "Missing $StartScript"
}

if ($AtStartup -and -not (Test-IsElevated)) {
    Write-Host @"
ERROR: -AtStartup needs an elevated PowerShell (拒绝访问 = 未用管理员运行).

Fix either:
  1) Right-click PowerShell -> Run as administrator, then:
       cd $Root
       .\scripts\register_autostart.ps1 -AtStartup

  2) Or use login autostart (no admin, recommended):
       .\scripts\register_autostart.ps1
"@ -ForegroundColor Yellow
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $Root

if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT1M"
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# Highest only when elevated; otherwise Limited avoids Access Denied under UAC
$runLevel = if (Test-IsElevated) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel $runLevel

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Auto-start QA Report Tool (uvicorn :8000, workers=1)" `
        -Force | Out-Null
} catch {
    Write-Host "ERROR: Register-ScheduledTask failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.Exception.Message -match "拒绝访问|Access is denied|0x80070005") {
        Write-Host "Hint: open PowerShell as Administrator, or run without -AtStartup." -ForegroundColor Yellow
    }
    exit 1
}

$when = if ($AtStartup) { "AtStartup (+1min delay)" } else { "AtLogOn ($env:USERNAME)" }
Write-Host "OK: registered task '$TaskName' -> $when (RunLevel=$runLevel)"
Write-Host "Start now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "Script:     $StartScript"
