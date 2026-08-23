param(
    [string]$ExePath = "",
    [ValidateSet("ColdLow", "FullThenLow", "Full")]
    [string]$Scenario = "ColdLow",
    [ValidateRange(10, 3600)]
    [int]$StableSeconds = 300,
    [ValidateRange(1, 60)]
    [int]$SampleIntervalSeconds = 5,
    [string]$OutputDirectory = "",
    [string]$InitialPath = "C:\"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$defaultExePath = Join-Path $projectRoot "dist\C盘空间增长监控器.exe"
$resolvedExePath = if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $defaultExePath
} elseif ([System.IO.Path]::IsPathRooted($ExePath)) {
    [System.IO.Path]::GetFullPath($ExePath)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ExePath))
}
$resolvedOutputDirectory = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $PSScriptRoot "results"
} elseif ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
}
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$measureRoot = Join-Path $tempBase ("DiskMonitorMemory-" + [guid]::NewGuid())
$logPath = Join-Path $measureRoot "DiskGrowthMonitor\ui.log"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME
$previousTestLowAfterBaseline = (
    $env:DISK_GROWTH_MONITOR_TEST_LOW_AFTER_BASELINE
)

function Get-TargetProcesses {
    return @(
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $resolvedExePath }
    )
}

function Wait-ForLogPattern {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Pattern,
        [int]$TimeoutSeconds = 180
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (
            (Test-Path -LiteralPath $logPath) -and
            (Select-String -LiteralPath $logPath -Pattern $Pattern -Quiet)
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "等待日志超时：$Pattern"
}

function Get-MetricSummary {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Samples,
        [Parameter(Mandatory = $true)]
        [string]$PropertyName
    )
    $values = @($Samples | ForEach-Object { [long]($_.$PropertyName) })
    $measure = $values | Measure-Object -Minimum -Maximum -Average
    return [ordered]@{
        min_bytes = [long]$measure.Minimum
        average_bytes = [long][Math]::Round($measure.Average)
        max_bytes = [long]$measure.Maximum
        final_bytes = [long]$values[-1]
        min_mb = [Math]::Round($measure.Minimum / 1MB, 1)
        average_mb = [Math]::Round($measure.Average / 1MB, 1)
        max_mb = [Math]::Round($measure.Maximum / 1MB, 1)
        final_mb = [Math]::Round($values[-1] / 1MB, 1)
    }
}

if (-not (Test-Path -LiteralPath $resolvedExePath -PathType Leaf)) {
    throw "找不到待测 EXE：$resolvedExePath"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "找不到项目 Python：$pythonPath"
}
if ((Get-TargetProcesses).Count -gt 0) {
    throw "目标 EXE 已在运行，不能进行隔离内存测量。"
}

New-Item -ItemType Directory -Path $measureRoot | Out-Null
New-Item -ItemType Directory -Force -Path $resolvedOutputDirectory | Out-Null
$env:LOCALAPPDATA = $measureRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $InitialPath
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorMemory-" + [guid]::NewGuid()
)
$env:DISK_GROWTH_MONITOR_TEST_LOW_AFTER_BASELINE = if (
    $Scenario -eq "FullThenLow"
) { "1" } else { $null }

try {
    $initialMode = if ($Scenario -eq "ColdLow") { "low_memory" } else { "full" }
    & $pythonPath -c (
        "from disk_monitor.storage import Storage; " +
        "s=Storage(); s.set_setting('close_behavior','quick'); " +
        "s.set_setting('run_mode','$initialMode')"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "初始化隔离设置失败。"
    }

    Start-Process `
        -FilePath $resolvedExePath `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden | Out-Null

    if ($Scenario -eq "ColdLow") {
        Wait-ForLogPattern -Pattern "low_memory_session_started" -TimeoutSeconds 30
    } else {
        Wait-ForLogPattern -Pattern "scan_ui_finished role=baseline" -TimeoutSeconds 180
    }
    if ($Scenario -eq "FullThenLow") {
        Wait-ForLogPattern -Pattern "run_mode_changed mode=low_memory" -TimeoutSeconds 30
    }

    $samples = [System.Collections.Generic.List[object]]::new()
    $stableStartedAt = Get-Date
    $stableDeadline = $stableStartedAt.AddSeconds($StableSeconds)
    while ((Get-Date) -le $stableDeadline) {
        $processes = Get-TargetProcesses
        if ($processes.Count -eq 0) {
            throw "内存测量期间目标 EXE 意外退出。"
        }
        $windowProcess = $processes |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            Select-Object -First 1
        if ($null -eq $windowProcess) {
            throw "内存测量期间未找到窗口进程。"
        }
        $samples.Add([pscustomobject]@{
            recorded_at = (Get-Date).ToString("o")
            process_count = $processes.Count
            window_working_set_bytes = [long]$windowProcess.WorkingSet64
            window_private_bytes = [long]$windowProcess.PrivateMemorySize64
            total_working_set_bytes = [long](
                ($processes | Measure-Object -Property WorkingSet64 -Sum).Sum
            )
            total_private_bytes = [long](
                ($processes | Measure-Object -Property PrivateMemorySize64 -Sum).Sum
            )
        })
        if ((Get-Date).AddSeconds($SampleIntervalSeconds) -gt $stableDeadline) {
            break
        }
        Start-Sleep -Seconds $SampleIntervalSeconds
    }

    $result = [ordered]@{
        scenario = $Scenario
        executable = $resolvedExePath
        stable_started_at = $stableStartedAt.ToString("o")
        stable_seconds = $StableSeconds
        sample_interval_seconds = $SampleIntervalSeconds
        sample_count = $samples.Count
        process_counts = @($samples.process_count | Sort-Object -Unique)
        window_working_set = Get-MetricSummary $samples "window_working_set_bytes"
        window_private_memory = Get-MetricSummary $samples "window_private_bytes"
        total_working_set = Get-MetricSummary $samples "total_working_set_bytes"
        total_private_memory = Get-MetricSummary $samples "total_private_bytes"
        samples = $samples
    }
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $outputPath = Join-Path $resolvedOutputDirectory (
        "memory-$Scenario-$timestamp.json"
    )
    $result | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath $outputPath -Encoding utf8

    "场景：$Scenario"
    "稳定采样：{0} 秒 / {1} 个样本" -f $StableSeconds, $samples.Count
    "窗口工作集：平均 {0:N1} MB，末值 {1:N1} MB，范围 {2:N1}–{3:N1} MB" -f `
        $result.window_working_set.average_mb, `
        $result.window_working_set.final_mb, `
        $result.window_working_set.min_mb, `
        $result.window_working_set.max_mb
    "窗口私有内存：平均 {0:N1} MB，末值 {1:N1} MB，范围 {2:N1}–{3:N1} MB" -f `
        $result.window_private_memory.average_mb, `
        $result.window_private_memory.final_mb, `
        $result.window_private_memory.min_mb, `
        $result.window_private_memory.max_mb
    "两进程合计工作集：平均 {0:N1} MB，末值 {1:N1} MB，范围 {2:N1}–{3:N1} MB" -f `
        $result.total_working_set.average_mb, `
        $result.total_working_set.final_mb, `
        $result.total_working_set.min_mb, `
        $result.total_working_set.max_mb
    "两进程合计私有内存：平均 {0:N1} MB，末值 {1:N1} MB，范围 {2:N1}–{3:N1} MB" -f `
        $result.total_private_memory.average_mb, `
        $result.total_private_memory.final_mb, `
        $result.total_private_memory.min_mb, `
        $result.total_private_memory.max_mb
    "结果文件：$outputPath"

    $windowProcess = Get-TargetProcesses |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1
    if ($null -eq $windowProcess -or -not $windowProcess.CloseMainWindow()) {
        throw "内存测量完成后，主窗口未接受正常关闭请求。"
    }
    $closeDeadline = (Get-Date).AddSeconds(20)
    do {
        $remaining = Get-TargetProcesses
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $closeDeadline)
    if ($remaining.Count -gt 0) {
        throw "内存测量实例未在 20 秒内正常退出。"
    }
    $expectedEndReason = if ($Scenario -eq "Full") {
        "quick_close"
    } else {
        "low_memory_close"
    }
    & $pythonPath -c (
        "from disk_monitor.storage import Storage; " +
        "s=Storage(); session=s.latest_completed_session(); " +
        "assert session is not None; " +
        "assert session.end_reason == '$expectedEndReason', session; " +
        "assert session.end_snapshot_id is None, session"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "隔离测量会话的关闭语义校验失败。"
    }
}
finally {
    Get-TargetProcesses | Stop-Process -Force
    $stopDeadline = (Get-Date).AddSeconds(5)
    while ((Get-TargetProcesses).Count -gt 0 -and (Get-Date) -lt $stopDeadline) {
        Start-Sleep -Milliseconds 100
    }
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:DISK_GROWTH_MONITOR_INITIAL_PATH = $previousInitialPath
    $env:DISK_GROWTH_MONITOR_INSTANCE_NAME = $previousInstanceName
    $env:DISK_GROWTH_MONITOR_TEST_LOW_AFTER_BASELINE = (
        $previousTestLowAfterBaseline
    )
    $resolvedMeasureRoot = [System.IO.Path]::GetFullPath($measureRoot)
    if (
        $resolvedMeasureRoot.StartsWith($tempBase) -and
        (Split-Path -Leaf $resolvedMeasureRoot).StartsWith("DiskMonitorMemory-")
    ) {
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            Remove-Item `
                -LiteralPath $resolvedMeasureRoot `
                -Recurse `
                -Force `
                -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $resolvedMeasureRoot)) {
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if (Test-Path -LiteralPath $resolvedMeasureRoot) {
            Write-Warning "隔离测量目录未能自动清理：$resolvedMeasureRoot"
        }
    }
}
