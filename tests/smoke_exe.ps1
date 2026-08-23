param(
    [string]$ExePath = "",
    [ValidateSet("quick", "full")]
    [string]$CloseBehavior = "quick"
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
$smokeRoot = Join-Path $tempBase ("DiskMonitorExeSmoke-" + [guid]::NewGuid())
$scanRoot = Join-Path $smokeRoot "scan-fixture"
$logPath = Join-Path $smokeRoot "DiskGrowthMonitor\ui.log"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME

$existingProcesses = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exePath }
if ($existingProcesses) {
    throw "检测到监控器正在运行，请先正常关闭后再执行 EXE 冒烟测试。"
}

New-Item -ItemType Directory -Path $smokeRoot | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scanRoot "first") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scanRoot "second") | Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "first\alpha.bin"),
    [byte[]]::new(16384)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "second\beta.bin"),
    [byte[]]::new(8192)
)
$env:LOCALAPPDATA = $smokeRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $scanRoot
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorSmoke-" + [guid]::NewGuid()
)

try {
    & $pythonPath -c "from disk_monitor.storage import Storage; Storage().set_setting('close_behavior', '$CloseBehavior')"

    $launcher = Start-Process `
        -FilePath $exePath `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru

    $windowProcess = $null
    $startDeadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $startDeadline) {
        $windowProcess = Get-Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Path -eq $exePath -and $_.MainWindowHandle -ne 0
            } |
            Select-Object -First 1
        if ($null -ne $windowProcess) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $windowProcess) {
        throw "EXE 未在 15 秒内创建主窗口。"
    }

    $baselineReady = $false
    $baselineDeadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $baselineDeadline) {
        $baselineState = & $pythonPath -c "import sqlite3; from disk_monitor.storage import default_database_path; c=sqlite3.connect(default_database_path()); row=c.execute(`"select count(*), sum(case when start_snapshot_id is not null then 1 else 0 end) from monitor_sessions where status='active'`" ).fetchone(); print(f'{row[0]}:{row[1] or 0}'); c.close()"
        $hasBaselineLog = Test-Path -LiteralPath $logPath
        $hasTreemapLog = $false
        $hasFinishedLog = $false
        if ($hasBaselineLog) {
            $hasTreemapLog = [bool](Select-String `
                -LiteralPath $logPath `
                -Pattern "treemap_rendered rectangles=[1-9][0-9]*" `
                -Quiet)
            $hasFinishedLog = [bool](Select-String `
                -LiteralPath $logPath `
                -Pattern "scan_ui_finished role=baseline" `
                -Quiet)
        }
        if (
            $baselineState -eq "1:1" -and
            $hasTreemapLog -and
            $hasFinishedLog
        ) {
            $baselineReady = $true
            break
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $baselineReady) {
        $logTail = if (Test-Path -LiteralPath $logPath) {
            (Get-Content -LiteralPath $logPath -Tail 20) -join [Environment]::NewLine
        } else {
            "ui.log 不存在"
        }
        throw "EXE 未在 30 秒内完成启动基线和非空矩形图。状态：$baselineState`n$logTail"
    }

    if (-not $windowProcess.CloseMainWindow()) {
        throw "主窗口未接受正常关闭请求。"
    }

    $closeDeadline = (Get-Date).AddSeconds(15)
    do {
        $remaining = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $exePath }
        if (-not $remaining) {
            break
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $closeDeadline)

    if ($remaining) {
        throw "EXE 收到关闭请求后仍未在 15 秒内退出。"
    }

    $sessionStatus = & $pythonPath -c "from disk_monitor.storage import Storage; s=Storage().latest_completed_session(); print(f'{s.status}:{s.end_reason}' if s else 'missing')"
    $expectedStatus = if ($CloseBehavior -eq "full") {
        "completed:normal_close"
    } else {
        "completed:quick_close"
    }
    if ($sessionStatus -ne $expectedStatus) {
        throw "关闭会话未正常保存：$sessionStatus"
    }
    if ($CloseBehavior -eq "full") {
        $endSnapshotId = & $pythonPath -c "from disk_monitor.storage import Storage; s=Storage().latest_completed_session(); print(s.end_snapshot_id or '') if s else print('')"
        if ($endSnapshotId -notmatch "^[1-9][0-9]*$") {
            throw "完整关闭未保存结束快照：$endSnapshotId"
        }
        $hasClosingLog = [bool](Select-String `
            -LiteralPath $logPath `
            -Pattern "scan_ui_finished role=closing outcome=success snapshot_id=$endSnapshotId" `
            -SimpleMatch `
            -Quiet)
        if (-not $hasClosingLog) {
            throw "完整关闭缺少 closing 扫描收尾日志。"
        }
    }
    Write-Host "EXE 启动基线、非空矩形图与 $CloseBehavior 关闭冒烟测试通过。"
}
finally {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath } |
        Stop-Process -Force
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:DISK_GROWTH_MONITOR_INITIAL_PATH = $previousInitialPath
    $env:DISK_GROWTH_MONITOR_INSTANCE_NAME = $previousInstanceName
    $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
    if (
        $resolvedSmokeRoot.StartsWith($tempBase) -and
        (Split-Path -Leaf $resolvedSmokeRoot).StartsWith("DiskMonitorExeSmoke-")
    ) {
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}
