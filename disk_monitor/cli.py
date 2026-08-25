from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .control_protocol import ControlError, error_response, success_response
from .control_transport import ControlClient
from .readonly import ReadOnlyDatabase
from .service import read_disk_sample
from .storage import default_database_path


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--control-directory", type=Path)
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(prog="diskmonitor")
    parser.add_argument("--version", action="version", version=__version__)
    groups = parser.add_subparsers(dest="group", required=True)

    app = groups.add_parser("app")
    app_commands = app.add_subparsers(dest="action", required=True)
    start = app_commands.add_parser("start", parents=[common])
    start.add_argument("--activate", action="store_true")
    app_commands.add_parser("status", parents=[common])
    app_commands.add_parser("activate", parents=[common])
    close = app_commands.add_parser("close", parents=[common])
    close.add_argument("--behavior", choices=("full", "quick"), required=True)

    mode = groups.add_parser("mode")
    mode_commands = mode.add_subparsers(dest="action", required=True)
    mode_commands.add_parser("get", parents=[common])
    mode_set = mode_commands.add_parser("set", parents=[common])
    mode_set.add_argument("mode", choices=("full", "low_memory"))
    mode_set.add_argument("--rescan", choices=("now", "later"))

    automation = groups.add_parser("automation")
    automation_commands = automation.add_subparsers(dest="action", required=True)
    automation_commands.add_parser("status", parents=[common])
    automation_configure = automation_commands.add_parser(
        "configure", parents=[common]
    )
    automation_configure.add_argument("--enabled", choices=("on", "off"))
    automation_configure.add_argument("--processes")
    automation_configure.add_argument(
        "--memory-pressure", choices=("on", "off")
    )
    automation_configure.add_argument("--high", type=int)
    automation_configure.add_argument("--low", type=int)
    automation_configure.add_argument(
        "--resume-rescan", choices=("now", "later")
    )

    scan = groups.add_parser("scan")
    scan_commands = scan.add_subparsers(dest="action", required=True)
    scan_start = scan_commands.add_parser("start", parents=[common])
    scan_start.add_argument("path")
    scan_commands.add_parser("rescan", parents=[common])
    scan_status = scan_commands.add_parser("status", parents=[common])
    scan_status.add_argument("--request-id")
    scan_wait = scan_commands.add_parser("wait", parents=[common])
    scan_wait.add_argument("--request-id", required=True)
    scan_wait.add_argument("--timeout", type=float, default=300.0)
    scan_commands.add_parser("cancel", parents=[common])
    scan_result = scan_commands.add_parser("result", parents=[common])
    scan_result.add_argument("--request-id", required=True)

    view = groups.add_parser("view")
    view_commands = view.add_subparsers(dest="action", required=True)
    view_commands.add_parser("current", parents=[common])
    view_open = view_commands.add_parser("open", parents=[common])
    view_open.add_argument("path")
    view_open.add_argument("--scan-if-missing", action="store_true")

    snapshot = groups.add_parser("snapshot")
    snapshot_commands = snapshot.add_subparsers(dest="action", required=True)
    snapshot_save = snapshot_commands.add_parser("save", parents=[common])
    snapshot_save.add_argument("--note", required=True)
    snapshot_list = snapshot_commands.add_parser("list", parents=[common])
    snapshot_list.add_argument("--limit", type=int, default=200)
    snapshot_list.add_argument("--path")
    snapshot_show = snapshot_commands.add_parser("show", parents=[common])
    snapshot_show.add_argument("snapshot_id", type=int)
    snapshot_compare = snapshot_commands.add_parser("compare", parents=[common])
    snapshot_compare.add_argument("new_snapshot_id", type=int)
    snapshot_compare.add_argument("old_snapshot_id", type=int)
    snapshot_compare.add_argument(
        "--direction", choices=("increase", "decrease"), default="increase"
    )
    snapshot_compare.add_argument("--limit", type=int, default=100)

    disk = groups.add_parser("disk")
    disk_commands = disk.add_subparsers(dest="action", required=True)
    disk_status = disk_commands.add_parser("status", parents=[common])
    disk_status.add_argument("--path", default="C:\\")

    session = groups.add_parser("session")
    session_commands = session.add_subparsers(dest="action", required=True)
    session_commands.add_parser("current", parents=[common])
    session_commands.add_parser("last", parents=[common])

    growth = groups.add_parser("growth")
    growth_commands = growth.add_subparsers(dest="action", required=True)
    for name in ("current", "last"):
        growth_parser = growth_commands.add_parser(name, parents=[common])
        growth_parser.add_argument(
            "--direction", choices=("increase", "decrease"), default="increase"
        )
        growth_parser.add_argument("--limit", type=int, default=100)

    tree = groups.add_parser("tree")
    tree_commands = tree.add_subparsers(dest="action", required=True)
    tree_current = tree_commands.add_parser("current", parents=[common])
    tree_current.add_argument("--limit", type=int, default=100)
    tree_snapshot = tree_commands.add_parser("snapshot", parents=[common])
    tree_snapshot.add_argument("snapshot_id", type=int)
    tree_snapshot.add_argument("--path")
    tree_snapshot.add_argument("--limit", type=int, default=100)

    report = groups.add_parser("report")
    report_commands = report.add_subparsers(dest="action", required=True)
    report_export = report_commands.add_parser("export", parents=[common])
    report_export.add_argument(
        "--format", choices=("json", "markdown", "csv"), default="markdown"
    )
    report_export.add_argument("--output", type=Path)
    report_export.add_argument("--force", action="store_true")
    report_export.add_argument("--limit", type=int, default=100)

    return parser


def _database(args: argparse.Namespace) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(args.database or default_database_path())


def _control_directory(args: argparse.Namespace) -> Path:
    return Path(args.control_directory or default_database_path().parent)


def _client(args: argparse.Namespace) -> ControlClient:
    return ControlClient(_control_directory(args))


def _launch_gui_process() -> None:
    override = os.environ.get("DISK_GROWTH_MONITOR_GUI_EXE")
    if override:
        command = [override]
        working_directory = str(Path(override).resolve().parent)
    elif getattr(sys, "frozen", False):
        gui_exe = Path(sys.executable).with_name(
            f"disk-space-growth-monitor-v{__version__}.exe"
        )
        if not gui_exe.is_file():
            raise ControlError("not_found", f"找不到 GUI 程序：{gui_exe}")
        command = [str(gui_exe)]
        working_directory = str(gui_exe.parent)
    else:
        project_root = Path(__file__).resolve().parent.parent
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = [str(pythonw if pythonw.is_file() else Path(sys.executable)), str(project_root / "run.py")]
        working_directory = str(project_root)
    subprocess.Popen(
        command,
        cwd=working_directory,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _app_start(args: argparse.Namespace, request_id: str) -> dict[str, Any]:
    client = _client(args)
    current = client.request("app.status")
    if current["ok"]:
        if args.activate:
            client.request("app.activate")
        return success_response(
            request_id,
            code="already_running",
            message="GUI 已在运行",
            data={**current.get("data", {}), "running": True},
        )
    if current["code"] != "gui_unavailable":
        return current
    _launch_gui_process()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        time.sleep(0.2)
        current = client.request("app.status")
        if current["ok"]:
            if args.activate:
                client.request("app.activate")
            return success_response(
                request_id,
                code="started",
                message="GUI 已启动",
                data={**current.get("data", {}), "running": True},
            )
    return error_response(request_id, "gui_unavailable", "GUI 未在规定时间内启动")


def _runtime_request(
    args: argparse.Namespace,
    command: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _client(args).request(command, payload or {})


def _disk_status(args: argparse.Namespace, request_id: str) -> dict[str, Any]:
    sample = read_disk_sample(args.path)
    data: dict[str, Any] = {
        "recorded_at": sample.recorded_at.isoformat(timespec="seconds"),
        "drive": sample.drive,
        "total_bytes": sample.total_bytes,
        "used_bytes": sample.used_bytes,
        "free_bytes": sample.free_bytes,
    }
    database_path = args.database or default_database_path()
    if Path(database_path).is_file():
        database = ReadOnlyDatabase(database_path)
        data["configured_mode"] = database.get_setting("run_mode", "full")
        data["latest_stored_sample"] = database.latest_disk_sample(sample.drive)
    return success_response(request_id, data=data)


def _last_session_response(
    args: argparse.Namespace,
    request_id: str,
    *,
    include_growth: bool,
) -> dict[str, Any]:
    database = _database(args)
    session = database.latest_completed_session()
    if session is None:
        return error_response(request_id, "not_found", "没有已完成的监控会话")
    data: dict[str, Any] = {"session": session}
    if include_growth:
        data.update(
            {
                "direction": args.direction,
                "items": database.session_growth(
                    session["id"],
                    direction=args.direction,
                    limit=args.limit,
                ),
            }
        )
    return success_response(request_id, data=data)


def _build_report(database: ReadOnlyDatabase, limit: int) -> dict[str, Any]:
    session = database.latest_completed_session()
    return {
        "generated_from": str(database.database_path),
        "session": session,
        "growth": (
            database.session_growth(session["id"], limit=limit) if session else []
        ),
        "snapshots": database.list_snapshots(limit=min(limit, 200)),
    }


def _render_report(data: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if output_format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=(
                "path",
                "name",
                "kind",
                "old_size_bytes",
                "new_size_bytes",
                "change_bytes",
            ),
        )
        writer.writeheader()
        for item in data["growth"]:
            writer.writerow({key: item.get(key, "") for key in writer.fieldnames})
        return output.getvalue()
    session = data["session"]
    lines = ["# 磁盘空间增长报告", ""]
    if session is None:
        lines.append("暂无已完成会话。")
    else:
        lines.extend(
            [
                f"- 会话：{session['id']}",
                f"- 路径：{session['root_path']}",
                f"- 开始：{session['started_at']}",
                f"- 结束：{session['ended_at']}",
                f"- 磁盘已用量变化：{session['change_bytes']} 字节",
                "",
                "## 文件快照增长",
                "",
            ]
        )
        if not data["growth"]:
            lines.append("本次会话没有可用的文件地址增长明细。")
        for item in data["growth"]:
            lines.append(f"- {item['path']}：{item['change_bytes']} 字节")
    return "\n".join(lines) + "\n"


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    try:
        if args.group == "app":
            if args.action == "start":
                return _app_start(args, request_id)
            if args.action == "status":
                response = _runtime_request(args, "app.status")
                if response["code"] == "gui_unavailable":
                    return success_response(
                        request_id,
                        code="not_running",
                        message="GUI 未运行",
                        data={"running": False},
                    )
                return response
            if args.action == "activate":
                return _runtime_request(args, "app.activate")
            return _runtime_request(
                args, "app.close", {"behavior": args.behavior}
            )
        if args.group == "mode":
            if args.action == "get":
                return _runtime_request(args, "mode.get")
            if args.mode == "full" and args.rescan is None:
                raise ControlError(
                    "invalid_args", "切回全功能模式必须指定 --rescan now|later"
                )
            return _runtime_request(
                args,
                "mode.set",
                {"mode": args.mode, "rescan": args.rescan},
            )
        if args.group == "automation":
            if args.action == "status":
                return _runtime_request(args, "automation.status")
            payload: dict[str, Any] = {}
            if args.enabled is not None:
                payload["enabled"] = args.enabled == "on"
            if args.processes is not None:
                payload["process_names"] = args.processes
            if args.memory_pressure is not None:
                payload["memory_pressure_enabled"] = (
                    args.memory_pressure == "on"
                )
            if args.high is not None:
                payload["high_percent"] = args.high
            if args.low is not None:
                payload["low_percent"] = args.low
            if args.resume_rescan is not None:
                payload["resume_rescan"] = args.resume_rescan
            return _runtime_request(args, "automation.configure", payload)
        if args.group == "scan":
            if args.action == "start":
                return _runtime_request(args, "scan.start", {"path": args.path})
            if args.action == "rescan":
                return _runtime_request(args, "scan.rescan")
            if args.action == "status":
                return _runtime_request(
                    args, "scan.status", {"request_id": args.request_id}
                )
            if args.action == "cancel":
                return _runtime_request(args, "scan.cancel")
            if args.action == "result":
                return _runtime_request(
                    args, "scan.result", {"request_id": args.request_id}
                )
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                response = _runtime_request(
                    args, "scan.status", {"request_id": args.request_id}
                )
                if not response["ok"]:
                    return response
                if response["data"].get("state") in {
                    "completed",
                    "cancelled",
                    "failed",
                }:
                    return response
                time.sleep(0.2)
            return error_response(request_id, "timeout", "等待扫描完成超时")
        if args.group == "view":
            if args.action == "current":
                return _runtime_request(args, "view.current")
            return _runtime_request(
                args,
                "view.open",
                {"path": args.path, "scan_if_missing": args.scan_if_missing},
            )
        if args.group == "snapshot":
            if args.action == "save":
                return _runtime_request(
                    args, "snapshot.save", {"note": args.note}
                )
            database = _database(args)
            if args.action == "list":
                return success_response(
                    request_id,
                    data={
                        "snapshots": database.list_snapshots(
                            limit=args.limit, root_path=args.path
                        )
                    },
                )
            if args.action == "show":
                snapshot = database.snapshot_info(args.snapshot_id)
                if snapshot is None:
                    return error_response(request_id, "not_found", "快照不存在")
                return success_response(request_id, data={"snapshot": snapshot})
            return success_response(
                request_id,
                data=database.compare_snapshots(
                    args.new_snapshot_id,
                    args.old_snapshot_id,
                    direction=args.direction,
                    limit=args.limit,
                ),
            )
        if args.group == "disk":
            return _disk_status(args, request_id)
        if args.group == "session":
            if args.action == "current":
                return _runtime_request(args, "session.current")
            return _last_session_response(
                args, request_id, include_growth=False
            )
        if args.group == "growth":
            if args.action == "current":
                return _runtime_request(
                    args,
                    "growth.current",
                    {"direction": args.direction, "limit": args.limit},
                )
            return _last_session_response(args, request_id, include_growth=True)
        if args.group == "tree":
            if args.action == "current":
                return _runtime_request(
                    args, "tree.current", {"limit": args.limit}
                )
            return success_response(
                request_id,
                data=_database(args).snapshot_tree(
                    args.snapshot_id, args.path, limit=args.limit
                ),
            )
        if args.group == "report":
            database = _database(args)
            report = _build_report(database, args.limit)
            content = _render_report(report, args.format)
            if args.output is not None:
                output_path = args.output.resolve()
                if output_path.exists() and not args.force:
                    raise ControlError(
                        "invalid_args", "报告文件已存在；使用 --force 才能覆盖"
                    )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(content, encoding="utf-8", newline="")
                return success_response(
                    request_id,
                    message="报告已导出",
                    data={"path": str(output_path), "format": args.format},
                )
            return success_response(
                request_id,
                data={"format": args.format, "content": content},
            )
        raise ControlError("invalid_args", "未知命令")
    except ControlError as error:
        return error_response(request_id, error.code, error.message)
    except OSError as error:
        return error_response(request_id, "path_unreadable", str(error))
    except Exception:
        return error_response(request_id, "internal_error", "CLI 命令执行失败")


def _print_human(response: dict[str, Any], *, report_command: bool = False) -> None:
    if not response["ok"]:
        print(f"错误 [{response['code']}]：{response['message']}", file=sys.stderr)
        return
    if report_command and "content" in response.get("data", {}):
        print(response["data"]["content"], end="")
        return
    if response["message"]:
        print(response["message"])
    if response.get("data"):
        print(json.dumps(response["data"], ensure_ascii=False, indent=2))


def _exit_code(response: dict[str, Any]) -> int:
    if response["ok"]:
        return 0
    if response["code"] == "invalid_args":
        return 2
    if response["code"] == "unauthorized":
        return 3
    if response["code"] in {
        "gui_unavailable",
        "not_found",
        "scan_busy",
        "low_memory_mode",
    }:
        return 4
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    response = dispatch(args)
    if getattr(args, "json_output", False):
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    else:
        _print_human(
            response,
            report_command=args.group == "report" and args.action == "export",
        )
    return _exit_code(response)
