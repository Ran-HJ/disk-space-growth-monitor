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
    --noupx `
    --version-file "version_info.txt" `
    --icon "assets\app.ico" `
    --add-data "assets\app.ico;assets" `
    --name "disk-space-growth-monitor-v0.7.2" `
    run.py

if ($LASTEXITCODE -ne 0) {
    throw "GUI 打包失败，退出代码：$LASTEXITCODE"
}

& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --console `
    --onefile `
    --noupx `
    --version-file "version_info_cli.txt" `
    --icon "assets\app.ico" `
    --name "diskmonitor-cli-v0.7.2" `
    run_cli.py

if ($LASTEXITCODE -ne 0) {
    throw "CLI 打包失败，退出代码：$LASTEXITCODE"
}

Write-Host "打包完成："
Write-Host "  dist\disk-space-growth-monitor-v0.7.2.exe"
Write-Host "  dist\diskmonitor-cli-v0.7.2.exe"
