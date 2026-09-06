param(
    # 留空测试源码；指定路径时只测试该打包 CLI，避免重复已通过的源码冒烟。
    [string]$CliExePath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$cliPath = if ($CliExePath) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CliExePath)
} else { "" }
if ($cliPath -and -not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    throw "找不到指定 CLI：$cliPath"
}
$guiPath = if ($cliPath) {
    Join-Path (Split-Path -Parent $cliPath) (
        (Split-Path -Leaf $cliPath).Replace("diskmonitor-cli-", "disk-space-growth-monitor-")
    )
} else { "" }
if ($cliPath -and (Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -eq $cliPath -or $_.Path -eq $guiPath
})) {
    throw "指定候选程序正在运行，请正常关闭后再执行冒烟。"
}

# subprocess 捕获原始字节后严格 UTF-8 解码；不依赖 PowerShell 的文本转码。
$smoke = @'
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

project = Path(sys.argv[1])
sys.path.insert(0, str(project))
from disk_monitor import __version__
from disk_monitor.control_protocol import PROTOCOL_VERSION
from disk_monitor.models import ScanItem, ScanResult
from disk_monitor.storage import Storage

command = [sys.argv[2]] if sys.argv[2] else [sys.executable, str(project / 'run_cli.py')]
mode = 'packaged' if sys.argv[2] else 'source'
allowed = {'ok', 'warning', 'error', 'unavailable'}

def metadata(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns

with tempfile.TemporaryDirectory(prefix='DiskMonitorDoctorSmoke-') as temporary:
    root = Path(temporary)
    environment = os.environ.copy()
    environment.update(
        LOCALAPPDATA=str(root / 'local-app-data'),
        TEMP=str(root), TMP=str(root),
        PYTHONIOENCODING='gbk',
        DISK_GROWTH_MONITOR_INSTANCE_NAME='Local\\DoctorSmoke-' + root.name,
        DISK_GROWTH_MONITOR_INITIAL_PATH=str(root / 'unused-scan-root'),
    )
    database = root / 'monitor.db'
    control = root / 'control'
    control.mkdir()
    secret = 'doctor-smoke-secret-never-output'
    pipe = r'\\.\pipe\DiskGrowthMonitor-doctor-smoke'
    endpoint = control / 'control.endpoint.json'
    authentication = control / 'control-fixture.auth'
    endpoint.write_text(json.dumps({
        'protocol_version': PROTOCOL_VERSION, 'instance_id': 'a' * 32,
        'pid': os.getpid(), 'pipe_address': pipe,
        'auth_file': authentication.name,
    }), encoding='utf-8')
    authentication.write_text(secret, encoding='utf-8')
    old_time = endpoint.stat().st_mtime - 3600
    os.utime(endpoint, (old_time, old_time))
    control_before = {p.name: metadata(p) for p in control.iterdir()}

    now = datetime.now().replace(microsecond=0)
    scan_root = root / 'synthetic-root'
    scan = ScanResult(
        root_path=str(scan_root), started_at=now, finished_at=now,
        total_bytes=7, file_count=1, directory_count=1, error_count=0,
        items=[ScanItem(str(scan_root), str(root), scan_root.name, 'directory', 7, 1, 0)],
    )
    Storage(database).save_scan(scan, source='closing')
    before = metadata(database)

    def invoke(arguments):
        result = subprocess.run(command + arguments, env=environment, cwd=project,
                                capture_output=True, timeout=30)
        stdout = result.stdout.decode('utf-8', errors='strict')
        stderr = result.stderr.decode('utf-8', errors='strict')
        assert result.returncode == 0, (result.returncode, stderr)
        assert stderr == '', stderr
        return stdout

    def doctor(path):
        text = invoke(['doctor', '--database', str(path),
                       '--control-directory', str(control), '--json'])
        for forbidden in ('auth_file', 'pipe_address', secret, authentication.name, pipe):
            assert forbidden not in text, 'Secret metadata leaked'
        response = json.loads(text)
        assert response['ok'] is True
        assert response['protocol_version'] == PROTOCOL_VERSION
        data = response['data']
        assert data['version'] == __version__
        assert data['protocol_version'] == PROTOCOL_VERSION
        assert data['overall_status'] in allowed
        assert set(data['checks']) == {
            'database', 'control', 'logging', 'file_information', 'latest_scan'
        }
        for check in data['checks'].values():
            assert check['status'] in allowed
        assert data['control']['process_alive'] is True
        assert data['control']['endpoint_stale'] is False
        assert data['control']['endpoint_present'] is True
        return data

    assert invoke(['--version']).strip() == __version__
    report = doctor(database)
    assert report['database']['status'] == 'ok'
    assert report['database']['read_only'] is True
    assert report['database']['schema_version'] == 3
    assert report['database']['quick_check'] == 'ok'
    assert report['database']['foreign_key_check'] == 'ok'
    assert report['database']['foreign_key_issue_count'] == 0
    assert report['database']['detail'] == '数据库只读检查通过'
    assert report['latest_scan']['source'] == 'closing'
    assert datetime.fromisoformat(report['latest_scan']['finished_at']) == now
    assert report['latest_scan']['error_count'] == 0
    assert report['latest_scan']['metadata_error_count'] == 0
    assert metadata(database) == before

    missing = root / 'missing-parent' / 'monitor.db'
    assert doctor(missing)['database']['status'] == 'unavailable'
    assert not missing.parent.exists()

    foreign = root / 'foreign-key.db'
    connection = sqlite3.connect(foreign)
    connection.executescript('''
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
        INSERT INTO child VALUES (99);
    ''')
    connection.commit()
    connection.close()
    foreign_before = metadata(foreign)
    foreign_report = doctor(foreign)['database']
    assert foreign_report['quick_check'] == 'ok'
    assert foreign_report['foreign_key_check'] == 'error'
    assert foreign_report['foreign_key_issue_count'] == 1
    assert metadata(foreign) == foreign_before
    assert {p.name: metadata(p) for p in control.iterdir()} == control_before
    assert not (root / 'local-app-data').exists()
    assert not (root / 'unused-scan-root').exists()
    assert not list(root.rglob('*.log'))

assert not Path(temporary).exists()
print(json.dumps({'ok': True, 'mode': mode, 'version': __version__,
                  'checks': ['schema_v3', 'read_only', 'latest_scan', 'missing_database',
                             'foreign_keys', 'secret_endpoint', 'strict_utf8', 'cleanup']},
                 ensure_ascii=True))
'@

& $pythonPath -c $smoke $projectRoot $cliPath
if ($LASTEXITCODE -ne 0) {
    throw "doctor 隔离冒烟失败，退出代码：$LASTEXITCODE"
}
if ($cliPath -and (Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -eq $cliPath -or $_.Path -eq $guiPath
})) {
    throw "doctor 冒烟后存在候选 CLI/GUI 进程残留。"
}
