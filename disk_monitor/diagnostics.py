from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import __version__
from .control_protocol import PROTOCOL_VERSION
from .control_transport import ENDPOINT_FILENAME, MAX_ENDPOINT_BYTES
from .storage import default_database_path
from .windows_file_info import file_information_api_status


CONTROL_STALE_SECONDS = 10 * 60
_STATUS_PRIORITY = {"ok": 0, "unavailable": 1, "warning": 2, "error": 3}


def _check(status: str, detail: str, **values: object) -> dict[str, object]:
    return {"status": status, "detail": detail, **values}


def _sqlite_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _latest_scan(connection: sqlite3.Connection) -> dict[str, object]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "snapshots" not in tables:
        return _check("unavailable", "数据库没有快照表")
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(snapshots)")
    }
    required = {"finished_at", "error_count"}
    if not required.issubset(columns):
        return _check("unavailable", "快照表字段不足，无法读取最近扫描")
    source = "source" if "source" in columns else "NULL"
    metadata_errors = (
        "metadata_error_count" if "metadata_error_count" in columns else "0"
    )
    row = connection.execute(
        f"""
        SELECT finished_at, {source} AS source, error_count,
               {metadata_errors} AS metadata_error_count
        FROM snapshots
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return _check("unavailable", "尚无已保存扫描")
    error_count = int(row["error_count"] or 0)
    metadata_error_count = int(row["metadata_error_count"] or 0)
    status = "warning" if error_count or metadata_error_count else "ok"
    return _check(
        status,
        "最近扫描存在读取或元数据错误"
        if status == "warning"
        else "已读取最近扫描摘要",
        finished_at=str(row["finished_at"]),
        source=str(row["source"] or "unknown"),
        error_count=error_count,
        metadata_error_count=metadata_error_count,
    )


def inspect_database(
    database_path: str | Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Inspect a database using SQLite read-only mode without schema assumptions."""

    path = Path(database_path)
    if not path.exists():
        database = _check(
            "unavailable", "监控数据库尚未创建", path=str(path), read_only=False
        )
        return database, _check("unavailable", "监控数据库尚未创建")
    if not path.is_file():
        database = _check(
            "error", "监控数据库路径不是文件", path=str(path), read_only=False
        )
        return database, _check("unavailable", "数据库不可读取")
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
    except sqlite3.Error:
        database = _check(
            "error", "无法以只读方式打开监控数据库", path=str(path), read_only=False
        )
        return database, _check("unavailable", "数据库不可读取")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchmany(101)
        quick_ok = quick_rows == ["ok"]
        foreign_ok = not foreign_rows
        status = "ok" if quick_ok and foreign_ok else "error"
        database = _check(
            status,
            "数据库只读检查通过" if status == "ok" else "数据库检查发现问题",
            path=str(path),
            read_only=True,
            schema_version=schema_version,
            quick_check="ok" if quick_ok else "error",
            foreign_key_check="ok" if foreign_ok else "error",
            foreign_key_issue_count=(
                len(foreign_rows) if len(foreign_rows) < 101 else ">=101"
            ),
        )
        return database, _latest_scan(connection)
    except (sqlite3.Error, TypeError, ValueError):
        database = _check(
            "error", "只读数据库检查未完成", path=str(path), read_only=True
        )
        return database, _check("unavailable", "数据库检查未完成")
    finally:
        connection.close()


def _windows_process_exists(pid: int) -> bool:
    """Query a Windows process handle without sending a signal."""

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
    except (AttributeError, OSError):
        return False
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == error_access_denied


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_control(control_directory: str | Path) -> dict[str, object]:
    """Read only non-secret endpoint metadata; never open the auth file."""

    directory = Path(control_directory)
    endpoint_path = directory / ENDPOINT_FILENAME
    if not endpoint_path.is_file():
        return _check(
            "unavailable", "未发现 GUI 控制端点", endpoint_present=False
        )
    try:
        with endpoint_path.open("rb") as handle:
            payload = handle.read(MAX_ENDPOINT_BYTES + 1)
        if len(payload) > MAX_ENDPOINT_BYTES:
            raise ValueError
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        pid = data.get("pid")
        protocol_version = data.get("protocol_version")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid < 1
            or pid > 0xFFFFFFFF
        ):
            raise ValueError
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _check("warning", "控制端点不可读取或格式无效", endpoint_present=True)
    try:
        endpoint_mtime = endpoint_path.stat().st_mtime
    except OSError:
        return _check("warning", "控制端点在读取期间已变化", endpoint_present=True)
    age_seconds = max(0, int(datetime.now().timestamp() - endpoint_mtime))
    process_alive = _process_exists(pid)
    compatible = protocol_version == PROTOCOL_VERSION
    endpoint_stale = not process_alive and age_seconds > CONTROL_STALE_SECONDS
    status = "ok"
    detail = "GUI 控制端点可用"
    if not compatible:
        status, detail = "warning", "控制端点协议版本不兼容"
    elif endpoint_stale:
        status, detail = "warning", "控制端点已陈旧且对应进程不存在"
    elif not process_alive:
        status, detail = "warning", "控制端点对应进程不存在"
    return _check(
        status,
        detail,
        endpoint_present=True,
        protocol_compatible=compatible,
        process_alive=process_alive,
        endpoint_stale=endpoint_stale,
        endpoint_age_seconds=age_seconds,
    )


def inspect_logging(log_directory: str | Path) -> dict[str, object]:
    """Check directory metadata only; do not create a probe file or directory."""

    directory = Path(log_directory)
    if not directory.is_dir():
        return _check(
            "unavailable", "日志目录尚未创建", directory=str(directory), writable=None
        )
    writable = os.access(directory, os.W_OK | os.X_OK)
    return _check(
        "ok" if writable else "warning",
        "权限检查允许写入（未创建探针文件）"
        if writable
        else "无法确认日志目录写入权限",
        directory=str(directory),
        writable=writable,
    )


def inspect_file_information() -> dict[str, object]:
    status, detail = file_information_api_status()
    return _check(status, detail)


def inspect_doctor(
    *,
    database_path: str | Path | None = None,
    control_directory: str | Path | None = None,
) -> dict[str, object]:
    """Return a non-mutating local diagnostic report suitable for CLI JSON."""

    database = Path(database_path or default_database_path())
    control_dir = Path(control_directory or database.parent)
    database_check, latest_scan = inspect_database(database)
    control = inspect_control(control_dir)
    logging_check = inspect_logging(database.parent)
    file_information = inspect_file_information()
    checks = {
        "database": database_check,
        "control": control,
        "logging": logging_check,
        "file_information": file_information,
        "latest_scan": latest_scan,
    }
    highest = max(
        (_STATUS_PRIORITY[str(check["status"])] for check in checks.values()),
        default=0,
    )
    status = next(
        name for name, priority in _STATUS_PRIORITY.items() if priority == highest
    )
    return {
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "overall_status": status,
        "database": database_check,
        "control": control,
        "logging": logging_check,
        "file_information": file_information,
        "latest_scan": latest_scan,
        "checks": checks,
    }
