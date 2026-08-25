from __future__ import annotations

import os
from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import __version__
from .control_protocol import (
    ControlError,
    error_response,
    success_response,
    utc_timestamp,
)
from .models import DiskSample, GrowthItem, ScanItem, ScanProgress, ScanResult

if TYPE_CHECKING:
    from .ui import DiskMonitorApp


TERMINAL_TASK_STATES = {"completed", "cancelled", "failed"}


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _sample_data(sample: DiskSample | None) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "recorded_at": _datetime_text(sample.recorded_at),
        "drive": sample.drive,
        "total_bytes": sample.total_bytes,
        "used_bytes": sample.used_bytes,
        "free_bytes": sample.free_bytes,
    }


def _scan_item_data(item: ScanItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "parent_path": item.parent_path,
        "name": item.name,
        "kind": item.kind,
        "size_bytes": item.size_bytes,
        "file_count": item.file_count,
        "depth": item.depth,
        "modified_at": item.modified_at,
    }


def _growth_item_data(item: GrowthItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "parent_path": item.parent_path,
        "name": item.name,
        "kind": item.kind,
        "old_size_bytes": item.old_size_bytes,
        "new_size_bytes": item.new_size_bytes,
        "change_bytes": item.change_bytes,
    }


def _scan_result_data(result: ScanResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "snapshot_id": result.snapshot_id,
        "root_path": result.root_path,
        "started_at": _datetime_text(result.started_at),
        "finished_at": _datetime_text(result.finished_at),
        "total_bytes": result.total_bytes,
        "file_count": result.file_count,
        "directory_count": result.directory_count,
        "error_count": result.error_count,
        "skeleton_degraded": bool(result.skeleton and result.skeleton.degraded),
    }


class GuiAgentController:
    """Whitelist of Agent operations executed by the Tk main thread."""

    def __init__(self, app: DiskMonitorApp) -> None:
        self.app = app
        self.tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.active_request_id: str | None = None

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["request_id"]
        try:
            command = request["command"]
            args = request.get("args", {})
            handlers = {
                "app.status": self._app_status,
                "app.activate": self._app_activate,
                "app.close": self._app_close,
                "mode.get": self._mode_get,
                "mode.set": self._mode_set,
                "automation.status": self._automation_status,
                "automation.configure": self._automation_configure,
                "scan.start": self._scan_start,
                "scan.rescan": self._scan_rescan,
                "scan.status": self._scan_status,
                "scan.cancel": self._scan_cancel,
                "scan.result": self._scan_result,
                "view.current": self._view_current,
                "view.open": self._view_open,
                "snapshot.save": self._snapshot_save,
                "session.current": self._session_current,
                "growth.current": self._growth_current,
                "tree.current": self._tree_current,
            }
            handler = handlers.get(command)
            if handler is None:
                raise ControlError("invalid_args", f"不支持的控制命令：{command}")
            return handler(request_id, args)
        except ControlError as error:
            return error_response(request_id, error.code, error.message)
        except (OSError, ValueError) as error:
            return error_response(request_id, "invalid_args", str(error))
        except Exception:
            self.app.logger.exception(
                "agent_control_failed command=%s", request.get("command")
            )
            return error_response(
                request_id, "internal_error", "GUI 控制请求处理失败"
            )

    def _require_ready(self) -> None:
        if self.app.session_id is None or self.app.session_start_sample is None:
            raise ControlError("gui_unavailable", "GUI 正在初始化，请稍后重试")
        if self.app.closing:
            raise ControlError("gui_unavailable", "GUI 正在关闭")

    def _require_full_mode(self) -> None:
        if self.app.run_mode == "low_memory":
            raise ControlError(
                "low_memory_mode",
                "低内存模式不执行目录扫描；请先显式切换到全功能模式",
            )

    def _require_idle_scan(self) -> None:
        if self.app.active_scan_role is not None or (
            self.app.scan_thread and self.app.scan_thread.is_alive()
        ):
            raise ControlError("scan_busy", "已有目录扫描正在进行")

    @staticmethod
    def _normalize_existing_directory(path: object) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ControlError("invalid_args", "目录路径不能为空")
        normalized = os.path.normcase(os.path.abspath(path.strip()))
        if not os.path.isdir(normalized):
            raise ControlError("path_unreadable", f"目录不存在或不可读：{normalized}")
        return normalized

    @staticmethod
    def _positive_limit(args: dict[str, Any], default: int = 100) -> int:
        limit = args.get("limit", default)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ControlError("invalid_args", "limit 必须是正整数")
        return min(limit, 2_000)

    def _app_status(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        return success_response(
            request_id,
            data={
                "running": True,
                "version": __version__,
                "pid": os.getpid(),
                "mode": self.app.run_mode,
                "path": self.app.path_var.get(),
                "session_id": self.app.session_id,
                "closing": self.app.closing,
                "scan": self._current_task_data(),
                "automation": self.app._automation_data(),
                "tray_available": self.app.tray_icon is not None,
            },
        )

    def _app_activate(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        self.app.root.deiconify()
        self.app.root.lift()
        self.app.root.focus_force()
        return success_response(request_id, message="GUI 窗口已激活")

    def _app_close(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        behavior = args.get("behavior")
        if behavior not in {"full", "quick"}:
            raise ControlError("invalid_args", "behavior 必须是 full 或 quick")
        if self.app.closing:
            return success_response(
                request_id,
                code="already_closing",
                message="GUI 已在关闭流程中",
            )
        self.app.root.after_idle(
            lambda: self.app._request_controlled_close(behavior)
        )
        return success_response(
            request_id,
            message="已请求 GUI 关闭",
            data={"behavior": behavior},
        )

    def _mode_get(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        return success_response(
            request_id,
            data={
                "mode": self.app.run_mode,
                "changed_at": _datetime_text(self.app.low_memory_started_at),
                "automation": self.app._automation_data(),
            },
        )

    def _mode_set(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        self._require_ready()
        mode = args.get("mode")
        rescan = args.get("rescan")
        if mode not in {"full", "low_memory"}:
            raise ControlError("invalid_args", "mode 必须是 full 或 low_memory")
        if mode == "full" and rescan not in {"now", "later"}:
            raise ControlError(
                "invalid_args", "切回全功能模式必须指定 rescan=now 或 later"
            )
        if mode == "low_memory" and rescan is not None:
            raise ControlError("invalid_args", "切换低内存模式不接受 rescan")
        if mode == self.app.run_mode:
            self.app._note_manual_mode_change("agent")
            return success_response(
                request_id,
                code="already_set",
                message="运行模式未变化",
                data={"mode": mode},
            )
        if mode == "low_memory":
            self._require_idle_scan()
            self.app._enter_low_memory_mode()
            self.app._note_manual_mode_change("agent")
            return success_response(
                request_id,
                message="已切换到低内存模式",
                data={"mode": self.app.run_mode},
            )

        should_scan = rescan == "now"
        role = "baseline" if self.app.low_memory_origin == "cold" else "low_memory_resume"
        if should_scan:
            self._create_task(request_id, role, self.app.session_root_path)
        try:
            switched = self.app._leave_low_memory_mode(should_scan=should_scan)
        except Exception:
            if should_scan:
                self._fail_task(request_id, "切回全功能模式失败")
            raise
        if not switched:
            if should_scan:
                self._fail_task(request_id, "切回全功能模式被取消")
            raise ControlError("internal_error", "切回全功能模式失败")
        self.app._note_manual_mode_change("agent")
        return success_response(
            request_id,
            message="已切换到全功能模式",
            data={
                "mode": self.app.run_mode,
                "rescan": rescan,
                "scan": self.tasks.get(request_id),
            },
        )

    def _automation_status(
        self, request_id: str, _args: dict[str, Any]
    ) -> dict[str, Any]:
        return success_response(request_id, data=self.app._automation_data())

    def _automation_configure(
        self, request_id: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_ready()
        allowed = {
            "enabled",
            "process_names",
            "memory_pressure_enabled",
            "high_percent",
            "low_percent",
            "resume_rescan",
        }
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ControlError(
                "invalid_args", "未知自动模式参数：" + ", ".join(unknown)
            )
        data = self.app._update_automation_from_control(args)
        return success_response(
            request_id,
            message="自动模式设置已更新",
            data=data,
        )

    def _start_task_scan(
        self, request_id: str, *, role: str, path: str
    ) -> dict[str, Any]:
        self._require_ready()
        self._require_full_mode()
        self._require_idle_scan()
        normalized = self._normalize_existing_directory(path)
        self._create_task(request_id, role, normalized)
        self.app._start_scan(role=role, path=normalized)
        if self.app.active_scan_role != role:
            self._fail_task(request_id, "扫描未能启动")
            raise ControlError("scan_failed", "扫描未能启动")
        return success_response(
            request_id,
            message="扫描已启动",
            data=self.tasks[request_id],
        )

    def _scan_start(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path") or self.app.path_var.get()
        return self._start_task_scan(request_id, role="manual", path=path)

    def _scan_rescan(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        return self._start_task_scan(
            request_id, role="manual", path=self.app.path_var.get()
        )

    def _scan_status(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        requested = args.get("request_id")
        if requested is not None and not isinstance(requested, str):
            raise ControlError("invalid_args", "request_id 必须是字符串")
        task = self._task_data(requested)
        if task is None:
            if requested:
                raise ControlError("not_found", "未找到指定的扫描任务")
            task = self._current_task_data()
        return success_response(request_id, data=task or {"state": "idle"})

    def _scan_cancel(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        if self.app.active_scan_role is None or not (
            self.app.scan_thread and self.app.scan_thread.is_alive()
        ):
            raise ControlError("not_found", "当前没有可取消的扫描")
        self.app._cancel_scan()
        if self.active_request_id is not None:
            active_task = self.tasks.get(self.active_request_id)
            if active_task is not None:
                active_task["state"] = "cancelling"
                active_task["updated_at"] = utc_timestamp()
        task = self._current_task_data() or {
            "state": "cancelling",
            "role": self.app.active_scan_role,
        }
        task["state"] = "cancelling"
        return success_response(request_id, message="已请求取消扫描", data=task)

    def _scan_result(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        requested = args.get("request_id")
        if not isinstance(requested, str) or not requested:
            raise ControlError("invalid_args", "必须指定扫描 request_id")
        task = self.tasks.get(requested)
        if task is None:
            raise ControlError("not_found", "未找到指定的扫描任务")
        if task["state"] == "cancelled":
            return error_response(
                request_id, "scan_cancelled", "扫描已取消", data=dict(task)
            )
        if task["state"] == "failed":
            return error_response(
                request_id, "scan_failed", "扫描失败", data=dict(task)
            )
        return success_response(request_id, data=dict(task))

    def _view_current(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        return success_response(
            request_id,
            data={
                "mode": self.app.run_mode,
                "path": self.app.path_var.get(),
                "result": _scan_result_data(self.app.current_result),
            },
        )

    def _view_open(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        self._require_ready()
        self._require_full_mode()
        self._require_idle_scan()
        path = self._normalize_existing_directory(args.get("path"))
        scan_if_missing = args.get("scan_if_missing", False)
        if not isinstance(scan_if_missing, bool):
            raise ControlError("invalid_args", "scan_if_missing 必须是布尔值")
        self.app.nav_stack = self.app._path_chain(path)
        self.app.path_var.set(path)
        self.app._refresh_breadcrumbs()
        result = self.app._navigation_result_from_skeleton(path)
        source = "memory_skeleton"
        if result is None:
            result = self.app.nav_cache.get(path)
            source = "session_cache"
        if result is None and not any(
            self.app._path_is_within(path, root)
            for root in self.app.nav_invalidated_roots
        ):
            snapshot_id = self.app.storage.latest_snapshot_id(path)
            if snapshot_id is not None:
                result = self.app.storage.load_snapshot(snapshot_id)
                if result is not None:
                    self.app.nav_cache[path] = result
                    source = "history_snapshot"
        if result is not None:
            self.app._show_navigation_result(result, "Agent 查询")
            return success_response(
                request_id,
                data={"path": path, "source": source, "result": _scan_result_data(result)},
            )
        if not scan_if_missing:
            raise ControlError("not_found", "没有该目录的可用明细；可显式允许扫描")
        return self._start_task_scan(request_id, role="navigation", path=path)

    def _snapshot_save(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        note = args.get("note")
        if not isinstance(note, str) or not note.strip():
            raise ControlError("invalid_args", "手动快照备注不能为空")
        path = self._normalize_existing_directory(self.app.path_var.get())
        self.app.pending_scan_note = note.strip()
        try:
            return self._start_task_scan(
                request_id, role="manual_save", path=path
            )
        except Exception:
            self.app.pending_scan_note = None
            raise

    def _session_current(self, request_id: str, _args: dict[str, Any]) -> dict[str, Any]:
        start = self.app.session_start_sample
        latest = self.app.latest_disk_sample
        change = None
        if start is not None and latest is not None:
            change = latest.used_bytes - start.used_bytes
        return success_response(
            request_id,
            data={
                "id": self.app.session_id,
                "status": "closing" if self.app.closing else "active",
                "root_path": self.app.session_root_path,
                "start_snapshot_id": self.app.session_start_snapshot_id,
                "start_sample": _sample_data(start),
                "latest_sample": _sample_data(latest),
                "used_change_bytes": change,
            },
        )

    def _growth_current(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        direction = args.get("direction", "increase")
        if direction not in {"increase", "decrease"}:
            raise ControlError("invalid_args", "direction 必须是 increase 或 decrease")
        limit = self._positive_limit(args)
        growth: list[GrowthItem] = []
        context = self.app.change_context
        snapshot_ids: dict[str, Any] | None = None
        if context is not None and context[0] == "snapshots":
            _, new_id, old_id, root_path = context
            growth = self.app.storage.compare_snapshot_changes(
                new_id, old_id, direction=direction, limit=limit
            )
            snapshot_ids = {
                "new_snapshot_id": new_id,
                "old_snapshot_id": old_id,
                "root_path": root_path,
            }
        start = self.app.session_start_sample
        latest = self.app.latest_disk_sample
        low_start = self.app.low_memory_start_sample
        return success_response(
            request_id,
            data={
                "direction": direction,
                "snapshot_scope": snapshot_ids,
                "items": [_growth_item_data(item) for item in growth],
                "session_used_change_bytes": (
                    latest.used_bytes - start.used_bytes
                    if latest is not None and start is not None
                    else None
                ),
                "startup_blind_spot_change_bytes": (
                    self.app.blind_spot_result.change_bytes
                    if self.app.blind_spot_result is not None
                    else None
                ),
                "low_memory_change_bytes": (
                    latest.used_bytes - low_start.used_bytes
                    if latest is not None and low_start is not None
                    else None
                ),
                "sampled_at": _datetime_text(latest.recorded_at) if latest else None,
            },
        )

    def _tree_current(self, request_id: str, args: dict[str, Any]) -> dict[str, Any]:
        limit = self._positive_limit(args)
        result = self.app.current_result
        if result is None:
            raise ControlError("not_found", "当前没有目录明细")
        root = os.path.normcase(os.path.abspath(self.app.path_var.get()))
        items = [
            item
            for item in result.items
            if os.path.normcase(os.path.abspath(item.parent_path)) == root
        ]
        items.sort(key=lambda item: (-item.size_bytes, item.path))
        return success_response(
            request_id,
            data={
                "path": root,
                "snapshot_id": result.snapshot_id,
                "finished_at": _datetime_text(result.finished_at),
                "items": [_scan_item_data(item) for item in items[:limit]],
            },
        )

    def _create_task(self, request_id: str, role: str, path: str) -> None:
        now = utc_timestamp()
        self.tasks[request_id] = {
            "request_id": request_id,
            "state": "running",
            "role": role,
            "path": path,
            "started_at": now,
            "updated_at": now,
            "progress": None,
            "result": None,
            "error": None,
        }
        self.active_request_id = request_id
        while len(self.tasks) > 100:
            oldest_id, oldest = next(iter(self.tasks.items()))
            if (
                oldest_id == self.active_request_id
                or oldest["state"] not in TERMINAL_TASK_STATES
            ):
                break
            self.tasks.popitem(last=False)

    def _fail_task(self, request_id: str, error: str) -> None:
        task = self.tasks.get(request_id)
        if task is not None:
            task["state"] = "failed"
            task["error"] = error
            task["updated_at"] = utc_timestamp()
        if self.active_request_id == request_id:
            self.active_request_id = None

    def on_scan_progress(self, progress: ScanProgress) -> None:
        if self.active_request_id is None:
            return
        task = self.tasks.get(self.active_request_id)
        if task is None:
            return
        task["progress"] = {
            "current_path": progress.current_path,
            "bytes_seen": progress.bytes_seen,
            "file_count": progress.file_count,
            "directory_count": progress.directory_count,
            "error_count": progress.error_count,
        }
        task["updated_at"] = utc_timestamp()

    def on_scan_completed(self, role: str, result: ScanResult) -> None:
        if self.active_request_id is None:
            return
        task = self.tasks.get(self.active_request_id)
        if task is None or task["role"] != role:
            return
        task["state"] = "completed"
        task["result"] = _scan_result_data(result)
        task["updated_at"] = utc_timestamp()
        self.active_request_id = None

    def on_scan_cancelled(self, role: str) -> None:
        self._finish_active_task(role, "cancelled")

    def on_scan_failed(self, role: str, error: str) -> None:
        self._finish_active_task(role, "failed", error=error)

    def _finish_active_task(
        self, role: str, state: str, *, error: str | None = None
    ) -> None:
        if self.active_request_id is None:
            return
        task = self.tasks.get(self.active_request_id)
        if task is None or task["role"] != role:
            return
        task["state"] = state
        task["error"] = error
        task["updated_at"] = utc_timestamp()
        self.active_request_id = None

    def _task_data(self, request_id: str | None) -> dict[str, Any] | None:
        if request_id is None:
            return None
        task = self.tasks.get(request_id)
        return dict(task) if task is not None else None

    def _current_task_data(self) -> dict[str, Any] | None:
        if self.active_request_id is not None:
            return self._task_data(self.active_request_id)
        if self.app.active_scan_role is not None:
            return {
                "request_id": None,
                "state": "running",
                "role": self.app.active_scan_role,
                "path": self.app.path_var.get(),
                "origin": "gui",
            }
        if self.tasks:
            return dict(next(reversed(self.tasks.values())))
        return None
