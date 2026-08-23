$ErrorActionPreference = "Stop"

$buildPython = if (Test-Path -LiteralPath ".venv\Scripts\python.exe") {
    ".venv\Scripts\python.exe"
} else {
    "python"
}

& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --version-file "version_info.txt" `
    --icon "assets\app.ico" `
    --add-data "assets\app.ico;assets" `
    --name "C盘空间增长监控器" `
    run.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，退出代码：$LASTEXITCODE"
}

Write-Host "打包完成：dist\C盘空间增长监控器.exe"
