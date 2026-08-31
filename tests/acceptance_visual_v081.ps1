param(
    [switch]$LayoutOnly,
    [switch]$SearchOnly
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$exePath = Join-Path $projectRoot "dist\disk-space-growth-monitor-v0.8.1.exe"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$acceptanceRoot = Join-Path $tempBase ("DiskMonitorVisualAcceptance081-" + [guid]::NewGuid())
$scanRoot = Join-Path $acceptanceRoot "scan-fixture"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "找不到 v0.8.1 GUI EXE：$exePath"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "找不到项目 Python 环境：$pythonPath"
}
$existingProcesses = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exePath }
if ($existingProcesses) {
    throw "v0.8.1 GUI 正在运行，请先正常关闭后再开始隔离验收。"
}

New-Item -ItemType Directory -Path (Join-Path $scanRoot "deep\level2\level3") -Force |
    Out-Null
New-Item -ItemType Directory -Path (Join-Path $scanRoot "other") -Force |
    Out-Null
New-Item -ItemType Directory -Path (Join-Path $scanRoot "excluded") -Force |
    Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "deep\level2\level3\archive-candidate.bin"),
    [byte[]]::new(2097152)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "deep\level2\readme.log"),
    [byte[]]::new(131072)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "other\photo.jpg"),
    [byte[]]::new(1048576)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "excluded\ignored.bin"),
    [byte[]]::new(3145728)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "temporary.tmp"),
    [byte[]]::new(524288)
)
New-Item `
    -ItemType HardLink `
    -Path (Join-Path $scanRoot "other\archive-link.bin") `
    -Target (Join-Path $scanRoot "deep\level2\level3\archive-candidate.bin") |
    Out-Null

$env:LOCALAPPDATA = $acceptanceRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $scanRoot
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorVisualAcceptance081-" + [guid]::NewGuid()
)

try {
    & $pythonPath -c "from disk_monitor.storage import Storage; s=Storage(); s.set_setting('close_behavior', 'quick'); s.set_setting('run_mode', 'full'); s.set_setting('file_space_accounting', 'exact'); s.set_setting('exclude_rules', 'excluded/**\n*.tmp')"
    if ($LASTEXITCODE -ne 0) {
        throw "无法准备 v0.8.1 隔离验收设置。"
    }

    $focusedCheck = $LayoutOnly -or $SearchOnly
    $stageLabel = if ($SearchOnly) {
        "搜索复验"
    }
    elseif ($LayoutOnly) {
        "布局复验"
    }
    else {
        "阶段 1/2"
    }
    Write-Host "$stageLabel：已启动隔离验收，不会访问正式数据库。"
    if ($SearchOnly) {
        Write-Host "请在快照查找中直接输入 archive 并按回车；完成后正常关闭窗口。"
    }
    else {
        Write-Host "请检查当前快照查找与迁移建议的按钮布局；完成后正常关闭窗口。"
    }
    $first = Start-Process -FilePath $exePath -WorkingDirectory $projectRoot -PassThru
    $first.WaitForExit()

    if (-not $focusedCheck) {
        Write-Host "阶段 2/2：即将使用同一隔离数据库重新启动。"
        Write-Host "请检查重启后的快照历史、深层展开与历史查找；完成后正常关闭窗口。"
        $second = Start-Process -FilePath $exePath -WorkingDirectory $projectRoot -PassThru
        $second.WaitForExit()
    }
}
finally {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:DISK_GROWTH_MONITOR_INITIAL_PATH = $previousInitialPath
    $env:DISK_GROWTH_MONITOR_INSTANCE_NAME = $previousInstanceName
    $resolvedAcceptanceRoot = [System.IO.Path]::GetFullPath($acceptanceRoot)
    if (
        $resolvedAcceptanceRoot.StartsWith($tempBase) -and
        (Split-Path -Leaf $resolvedAcceptanceRoot).StartsWith(
            "DiskMonitorVisualAcceptance081-"
        )
    ) {
        Remove-Item `
            -LiteralPath $resolvedAcceptanceRoot `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
