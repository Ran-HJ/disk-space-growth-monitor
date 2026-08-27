param(
    [string]$GuiExePath = "",
    [string]$CliExePath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$guiExe = if ([string]::IsNullOrWhiteSpace($GuiExePath)) {
    Join-Path $projectRoot "dist\disk-space-growth-monitor-v0.7.5.exe"
} else {
    [System.IO.Path]::GetFullPath($GuiExePath)
}
$cliExe = if ([string]::IsNullOrWhiteSpace($CliExePath)) {
    Join-Path $projectRoot "dist\diskmonitor-cli-v0.7.5.exe"
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
$smokeRoot = Join-Path $tempBase ("DiskMonitorAgentSmoke-" + [guid]::NewGuid())
$scanRoot = Join-Path $smokeRoot "scan-fixture"
$childRoot = Join-Path $scanRoot "child"
$controlDirectory = Join-Path $smokeRoot "DiskGrowthMonitor"
$databasePath = Join-Path $controlDirectory "monitor.db"
$previousLocalAppData = $env:LOCALAPPDATA
$previousInitialPath = $env:DISK_GROWTH_MONITOR_INITIAL_PATH
$previousInstanceName = $env:DISK_GROWTH_MONITOR_INSTANCE_NAME
$previousGuiOverride = $env:DISK_GROWTH_MONITOR_GUI_EXE
$knownPids = [System.Collections.Generic.List[int]]::new()

function Invoke-CliJson {
    param(
        [string[]]$Arguments,
        [int[]]$ExpectedExitCodes = @(0)
    )
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

New-Item -ItemType Directory -Path $childRoot -Force | Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $scanRoot "root.bin"),
    [byte[]]::new(16384)
)
[System.IO.File]::WriteAllBytes(
    (Join-Path $childRoot "child.bin"),
    [byte[]]::new(8192)
)

$env:LOCALAPPDATA = $smokeRoot
$env:DISK_GROWTH_MONITOR_INITIAL_PATH = $scanRoot
$env:DISK_GROWTH_MONITOR_INSTANCE_NAME = (
    "Local\DiskGrowthMonitorAgentSmoke-" + [guid]::NewGuid()
)
$env:DISK_GROWTH_MONITOR_GUI_EXE = $guiExe

try {
    $started = Invoke-CliJson -Arguments @("app", "start", "--json")
    if (-not $started.ok -or $started.code -ne "started") {
        throw "CLI 未成功启动 GUI：$($started | ConvertTo-Json -Compress)"
    }
    $firstPid = [int]$started.data.pid
    $knownPids.Add($firstPid)

    $already = Invoke-CliJson -Arguments @("app", "start", "--json")
    if ($already.code -ne "already_running" -or [int]$already.data.pid -ne $firstPid) {
        throw "app start 幂等语义不正确。"
    }

    $endpoint = Get-Content `
        -LiteralPath (Join-Path $controlDirectory "control.endpoint.json") `
        -Raw |
        ConvertFrom-Json
    $authPath = Join-Path $controlDirectory $endpoint.auth_file
    $firstAuth = [System.IO.File]::ReadAllBytes($authPath)
    try {
        [System.IO.File]::WriteAllBytes($authPath, [byte[]]::new(32))
        $unauthorized = Invoke-CliJson `
            -Arguments @("mode", "get", "--json") `
            -ExpectedExitCodes @(3)
        if ($unauthorized.code -ne "unauthorized") {
            throw "错误认证密钥未返回 unauthorized。"
        }
    } finally {
        [System.IO.File]::WriteAllBytes($authPath, $firstAuth)
    }

    $low = Invoke-CliJson -Arguments @("mode", "set", "low_memory", "--json")
    if (-not $low.ok -or $low.data.mode -ne "low_memory") {
        throw "切换低内存模式失败。"
    }
    $rejected = Invoke-CliJson `
        -Arguments @("scan", "start", $scanRoot, "--json") `
        -ExpectedExitCodes @(4)
    if ($rejected.code -ne "low_memory_mode") {
        throw "低内存模式未明确拒绝扫描。"
    }
    $full = Invoke-CliJson `
        -Arguments @("mode", "set", "full", "--rescan", "later", "--json")
    if (-not $full.ok -or $full.data.mode -ne "full") {
        throw "切回全功能模式失败。"
    }

    $scan = Invoke-CliJson -Arguments @("scan", "start", $scanRoot, "--json")
    $scanId = [string]$scan.data.request_id
    $scanDone = Invoke-CliJson `
        -Arguments @("scan", "wait", "--request-id", $scanId, "--timeout", "30", "--json")
    if ($scanDone.data.state -ne "completed") {
        throw "Agent 扫描任务未完成。"
    }
    $tree = Invoke-CliJson -Arguments @("tree", "current", "--limit", "20", "--json")
    if (-not $tree.ok -or $tree.data.items.Count -lt 1) {
        throw "当前目录树查询没有返回明细。"
    }
    $opened = Invoke-CliJson `
        -Arguments @("view", "open", $childRoot, "--json")
    if (-not $opened.ok -or -not $opened.data.source) {
        throw "Agent 目录导航未命中现有扫描数据。"
    }

    $snapshot = Invoke-CliJson `
        -Arguments @("snapshot", "save", "--note", "packaged-agent-smoke", "--json")
    $snapshotId = [string]$snapshot.data.request_id
    $snapshotDone = Invoke-CliJson `
        -Arguments @("scan", "wait", "--request-id", $snapshotId, "--timeout", "30", "--json")
    if ($snapshotDone.data.state -ne "completed") {
        throw "Agent 手动快照任务未完成。"
    }
    $snapshots = Invoke-CliJson `
        -Arguments @("snapshot", "list", "--limit", "20", "--database", $databasePath, "--json")
    if (-not ($snapshots.data.snapshots | Where-Object { $_.note -eq "packaged-agent-smoke" })) {
        throw "只读快照查询未找到 Agent 保存的快照。"
    }

    $closed = Invoke-CliJson `
        -Arguments @("app", "close", "--behavior", "quick", "--json")
    if (-not $closed.ok) {
        throw "Agent 快速关闭请求失败。"
    }
    Wait-ProcessExit -ProcessId $firstPid

    $restarted = Invoke-CliJson -Arguments @("app", "start", "--json")
    $secondPid = [int]$restarted.data.pid
    $knownPids.Add($secondPid)
    $secondEndpoint = Get-Content `
        -LiteralPath (Join-Path $controlDirectory "control.endpoint.json") `
        -Raw |
        ConvertFrom-Json
    $secondAuth = [System.IO.File]::ReadAllBytes(
        (Join-Path $controlDirectory $secondEndpoint.auth_file)
    )
    if (
        [Convert]::ToBase64String($firstAuth) -eq
        [Convert]::ToBase64String($secondAuth)
    ) {
        throw "GUI 重启后认证密钥没有更新。"
    }

    Stop-Process -Id $secondPid -Force
    Wait-ProcessExit -ProcessId $secondPid
    $stale = Invoke-CliJson -Arguments @("app", "status", "--json")
    if ($stale.code -ne "not_running" -or $stale.data.running) {
        throw "崩溃后的陈旧端点未快速返回 not_running。"
    }

    $recovered = Invoke-CliJson -Arguments @("app", "start", "--json")
    $thirdPid = [int]$recovered.data.pid
    $knownPids.Add($thirdPid)
    if ((Get-ChildItem -LiteralPath $controlDirectory -Filter "control-*.auth").Count -ne 1) {
        throw "重启后未清理陈旧认证文件。"
    }
    Invoke-CliJson `
        -Arguments @("app", "close", "--behavior", "quick", "--json") |
        Out-Null
    Wait-ProcessExit -ProcessId $thirdPid

    $lastSession = Invoke-CliJson `
        -Arguments @("session", "last", "--database", $databasePath, "--json")
    if (-not $lastSession.ok -or -not $lastSession.data.session) {
        throw "GUI 关闭后的只读会话查询失败。"
    }
    Write-Host "双 EXE Agent 控制、认证、模式、扫描、导航、快照、重启与只读查询冒烟通过。"
}
finally {
    foreach ($processId in $knownPids) {
        Get-Process -Id $processId -ErrorAction SilentlyContinue |
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
        (Split-Path -Leaf $resolvedSmokeRoot).StartsWith("DiskMonitorAgentSmoke-")
    ) {
        Remove-Item `
            -LiteralPath $resolvedSmokeRoot `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
