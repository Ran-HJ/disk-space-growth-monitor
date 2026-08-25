param(
    [string]$GuiExePath = "",
    [string]$CliExePath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$guiExe = if ([string]::IsNullOrWhiteSpace($GuiExePath)) {
    Join-Path $projectRoot "dist\disk-space-growth-monitor-v0.7.3.exe"
} else {
    [System.IO.Path]::GetFullPath($GuiExePath)
}
$cliExe = if ([string]::IsNullOrWhiteSpace($CliExePath)) {
    Join-Path $projectRoot "dist\diskmonitor-cli-v0.7.3.exe"
} else {
    [System.IO.Path]::GetFullPath($CliExePath)
}
if (-not (Test-Path -LiteralPath $guiExe -PathType Leaf)) {
    throw "找不到 GUI EXE：$guiExe"
}
if (-not (Test-Path -LiteralPath $cliExe -PathType Leaf)) {
    throw "找不到 CLI EXE：$cliExe"
}

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$smokeRoot = Join-Path $tempBase ("DiskMonitorAutoTraySmoke-" + [guid]::NewGuid())
$scanRoot = Join-Path $smokeRoot "scan-fixture"
$controlDirectory = Join-Path $smokeRoot "DiskGrowthMonitor"
$logPath = Join-Path $controlDirectory "ui.log"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME
$previousGuiOverride = $env:DISK_GROWTH_MONITOR_GUI_EXE
$guiPid = 0

function Invoke-CliJson {
    param([string[]]$Arguments, [int[]]$ExpectedExitCodes = @(0))
    $output = & $cliExe @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($ExpectedExitCodes -notcontains $exitCode) {
        throw "CLI 退出代码异常：$exitCode；输出：$($output -join [Environment]::NewLine)"
    }
    $text = $output -join [Environment]::NewLine
    try {
        return $text | ConvertFrom-Json
    } catch {
        throw "CLI 未返回有效 JSON：$text"
    }
}

function Wait-AppState {
    param(
        [string]$Mode,
        [string]$AutomationStatus,
        [int]$Seconds
    )
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $status = Invoke-CliJson -Arguments @("app", "status", "--json")
        if (
            $status.data.mode -eq $Mode -and
            $status.data.automation.status -eq $AutomationStatus
        ) {
            return $status
        }
        Start-Sleep -Milliseconds 500
    }
    throw "自动状态未按时到达：mode=$Mode automation=$AutomationStatus"
}

function Wait-ProcessExit {
    param([int]$ProcessId, [int]$Seconds = 15)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "GUI 进程未在规定时间内退出：$ProcessId"
}

New-Item -ItemType Directory -Path $scanRoot -Force | Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "fixture.bin"),
    [byte[]]::new(32768)
)
$env:LOCALAPPDATA = $smokeRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $scanRoot
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorAutoTraySmoke-" + [guid]::NewGuid()
)
$env:DISK_GROWTH_MONITOR_GUI_EXE = $guiExe

try {
    $started = Invoke-CliJson -Arguments @("app", "start", "--json")
    if (-not $started.ok -or $started.code -ne "started") {
        throw "GUI 启动失败。"
    }
    $guiPid = [int]$started.data.pid
    if (-not $started.data.tray_available) {
        throw "打包版未启动 Windows 托盘。"
    }

    $guiProcessName = Split-Path -Leaf $guiExe
    $configured = Invoke-CliJson -Arguments @(
        "automation", "configure",
        "--enabled", "on",
        "--processes", $guiProcessName,
        "--memory-pressure", "off",
        "--high", "85",
        "--low", "75",
        "--resume-rescan", "later",
        "--json"
    )
    if (-not $configured.ok) {
        throw "自动模式配置失败。"
    }
    $autoLow = Wait-AppState `
        -Mode "low_memory" `
        -AutomationStatus "auto_low" `
        -Seconds 20
    if (-not $autoLow.data.automation.owns_low_mode) {
        throw "自动进入低内存后没有记录自动所有权。"
    }

    Invoke-CliJson -Arguments @(
        "automation", "configure",
        "--processes", "definitely-not-running.exe",
        "--json"
    ) | Out-Null
    $autoFull = Wait-AppState `
        -Mode "full" `
        -AutomationStatus "monitoring" `
        -Seconds 45
    if ($autoFull.data.scan -and $autoFull.data.scan.state -eq "running") {
        throw "later 策略不应在自动恢复后立即扫描。"
    }

    Invoke-CliJson -Arguments @(
        "mode", "set", "low_memory", "--json"
    ) | Out-Null
    $manualLow = Wait-AppState `
        -Mode "low_memory" `
        -AutomationStatus "manual_low" `
        -Seconds 15
    if ($manualLow.data.automation.owns_low_mode) {
        throw "人工低内存模式不应归自动控制所有。"
    }

    $disabled = Invoke-CliJson -Arguments @(
        "automation", "configure", "--enabled", "off", "--json"
    )
    if (
        $disabled.data.status -ne "disabled" -or
        (Invoke-CliJson -Arguments @("mode", "get", "--json")).data.mode -ne "low_memory"
    ) {
        throw "关闭自动功能不应擅自改变当前模式。"
    }

    Invoke-CliJson -Arguments @(
        "app", "close", "--behavior", "quick", "--json"
    ) | Out-Null
    Wait-ProcessExit -ProcessId $guiPid
    if (-not (Select-String -LiteralPath $logPath -Pattern "tray_started" -Quiet)) {
        throw "日志缺少 tray_started。"
    }
    if (-not (Select-String -LiteralPath $logPath -Pattern "tray_stopped" -Quiet)) {
        throw "日志缺少 tray_stopped。"
    }
    Write-Host "v0.7.3 双 EXE 自动进程触发、稳定恢复、人工优先、托盘生命周期冒烟通过。"
}
finally {
    if ($guiPid -gt 0) {
        Get-Process -Id $guiPid -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 300
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:DISK_GROWTH_MONITOR_INITIAL_PATH = $previousInitialPath
    $env:DISK_GROWTH_MONITOR_INSTANCE_NAME = $previousInstanceName
    $env:DISK_GROWTH_MONITOR_GUI_EXE = $previousGuiOverride
    $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
    if (
        $resolvedSmokeRoot.StartsWith($tempBase) -and
        (Split-Path -Leaf $resolvedSmokeRoot).StartsWith("DiskMonitorAutoTraySmoke-")
    ) {
        Remove-Item `
            -LiteralPath $resolvedSmokeRoot `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
