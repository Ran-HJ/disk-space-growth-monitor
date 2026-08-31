$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$exePath = Join-Path $projectRoot "dist\disk-space-growth-monitor-v0.8.0.exe"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$acceptanceRoot = Join-Path $tempBase ("DiskMonitorVisualAcceptance-" + [guid]::NewGuid())
$scanRoot = Join-Path $acceptanceRoot "scan-fixture"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "找不到 v0.8.0 GUI EXE：$exePath"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "找不到项目 Python 环境：$pythonPath"
}
$existingProcesses = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exePath }
if ($existingProcesses) {
    throw "v0.8.0 GUI 正在运行，请先正常关闭后再开始隔离验收。"
}

New-Item -ItemType Directory -Path (Join-Path $scanRoot "first") -Force |
    Out-Null
New-Item -ItemType Directory -Path (Join-Path $scanRoot "second") -Force |
    Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "first\alpha.bin"),
    [byte[]]::new(1048576)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "second\beta.bin"),
    [byte[]]::new(524288)
)
New-Item `
    -ItemType HardLink `
    -Path (Join-Path $scanRoot "second\alpha-link.bin") `
    -Target (Join-Path $scanRoot "first\alpha.bin") |
    Out-Null

$env:LOCALAPPDATA = $acceptanceRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $scanRoot
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorVisualAcceptance-" + [guid]::NewGuid()
)

try {
    & $pythonPath -c "from disk_monitor.storage import Storage; s=Storage(); s.set_setting('close_behavior', 'quick'); s.set_setting('run_mode', 'full'); s.set_setting('file_space_accounting', 'exact')"
    if ($LASTEXITCODE -ne 0) {
        throw "无法准备隔离验收设置。"
    }
    Write-Host "已启动隔离验收，不会访问正式数据库。"
    Write-Host "请等待扫描完成，再按验收文档检查界面；正常关闭窗口后临时数据会自动删除。"
    $process = Start-Process `
        -FilePath $exePath `
        -WorkingDirectory $projectRoot `
        -PassThru
    $process.WaitForExit()
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
            "DiskMonitorVisualAcceptance-"
        )
    ) {
        Remove-Item `
            -LiteralPath $resolvedAcceptanceRoot `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
