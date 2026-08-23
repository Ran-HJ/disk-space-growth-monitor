param(
    [string]$ExePath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$defaultExePath = Join-Path $projectRoot "dist\C盘空间增长监控器.exe"
$exePath = if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $defaultExePath
} elseif ([System.IO.Path]::IsPathRooted($ExePath)) {
    [System.IO.Path]::GetFullPath($ExePath)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ExePath))
}
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$measureRoot = Join-Path $tempBase ("DiskMonitorMemory-" + [guid]::NewGuid())
$logPath = Join-Path $measureRoot "DiskGrowthMonitor\ui.log"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME

$existingProcesses = @(Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exePath })
if ($existingProcesses.Count -gt 0) {
    throw "目标 EXE 已在运行，不能进行隔离内存测量。"
}

New-Item -ItemType Directory -Path $measureRoot | Out-Null
$env:LOCALAPPDATA = $measureRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = "C:\"
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorMemory-" + [guid]::NewGuid()
)

try {
    & $pythonPath -c "from disk_monitor.storage import Storage; Storage().set_setting('close_behavior', 'quick')"
    Start-Process `
        -FilePath $exePath `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden | Out-Null

    $peakTotal = 0L
    $peakWindow = 0L
    $ready = $false
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline) {
        $processes = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $exePath })
        if ($processes.Count -gt 0) {
            $total = ($processes | Measure-Object -Property WorkingSet64 -Sum).Sum
            $windowWorkingSet = ($processes |
                Where-Object { $_.MainWindowHandle -ne 0 } |
                Measure-Object -Property WorkingSet64 -Maximum).Maximum
            if ($null -ne $total) {
                $peakTotal = [Math]::Max($peakTotal, [long]$total)
            }
            if ($null -ne $windowWorkingSet) {
                $peakWindow = [Math]::Max($peakWindow, [long]$windowWorkingSet)
            }
        }
        if (Test-Path -LiteralPath $logPath) {
            $ready = [bool](Select-String `
                -LiteralPath $logPath `
                -Pattern "scan_ui_finished role=baseline" `
                -Quiet)
            if ($ready) {
                break
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) {
        throw "真实 C 盘基线未在 3 分钟内完成。"
    }

    $windowProcess = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath -and $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1
    if ($null -eq $windowProcess -or -not $windowProcess.CloseMainWindow()) {
        throw "内存测量完成后，主窗口未接受正常关闭请求。"
    }

    $closeDeadline = (Get-Date).AddSeconds(15)
    do {
        $remaining = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $exePath })
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $closeDeadline)
    if ($remaining.Count -gt 0) {
        throw "内存测量实例未在 15 秒内正常退出。"
    }

    "窗口进程峰值工作集：{0:N1} MB" -f ($peakWindow / 1MB)
    "单文件 EXE 两进程合计峰值工作集：{0:N1} MB" -f ($peakTotal / 1MB)
}
finally {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath } |
        Stop-Process -Force
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:DISK_GROWTH_MONITOR_INITIAL_PATH = $previousInitialPath
    $env:DISK_GROWTH_MONITOR_INSTANCE_NAME = $previousInstanceName
    $resolvedMeasureRoot = [System.IO.Path]::GetFullPath($measureRoot)
    if (
        $resolvedMeasureRoot.StartsWith($tempBase) -and
        (Split-Path -Leaf $resolvedMeasureRoot).StartsWith("DiskMonitorMemory-")
    ) {
        Remove-Item -LiteralPath $resolvedMeasureRoot -Recurse -Force
    }
}
