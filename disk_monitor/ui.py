from __future__ import annotations

import atexit
import gc
import logging
import os
import queue
import sqlite3
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .agent_control import GuiAgentController
from .automation import (
    AutoDecision,
    AutoModeConfig,
    AutoModePolicy,
    AutoObservation,
    WindowsSystemProbe,
    normalize_process_names,
)
from .autostart import is_autostart_enabled, set_autostart
from .control_bridge import GuiControlBridge
from .control_protocol import ControlError
from .formatting import format_bytes
from .growth_tree import GrowthTreeNode, build_growth_tree
from .models import (
    BlindSpotResult,
    DirectorySkeleton,
    DiskSample,
    GrowthItem,
    ScanItem,
    ScanProgress,
    ScanResult,
    SessionBoundary,
    SnapshotInfo,
)
from .migration_advice import SAFETY_NOTICE, build_migration_advice
from .navigation import (
    NAVIGATION_MEMORY_BUDGET_BYTES,
    materialize_navigation_result,
    merge_directory_skeleton,
)
from .readonly import ReadOnlyDatabase
from .scanner import ScanCancelled, scan_path
from .settings_view import (
    SettingsDialog,
    SettingsDialogState,
    SettingsSubmission,
)
from .service import (
    calculate_blind_spot,
    continuous_baseline_snapshot_id,
    nearest_disk_sample,
    normalize_drive,
    read_disk_sample,
)
from .storage import Storage
from .tray import TrayState, WindowsTrayIcon
from .trend_view import build_trend_geometry
from .treemap_view import item_at as treemap_item_at
from .treemap_view import layout_rectangles
from .windows_display import (
    DisplayMetrics,
    cursor_work_area,
    enable_per_monitor_dpi_awareness,
    position_near_cursor,
    sync_tk_scaling,
)


COLORS = {
    "background": "#f3f5f8",
    "panel": "#ffffff",
    "text": "#172033",
    "muted": "#657086",
    "accent": "#2563eb",
    "accent_light": "#dbeafe",
    "directory": "#f6c453",
    "directory_alt": "#f59e0b",
    "file": "#60a5fa",
    "border": "#d8dee9",
    "positive": "#dc2626",
    "warning": "#d97706",
}

CLOSE_BEHAVIOR_LABELS = {
    "ask": "每次询问",
    "full": "始终完整保存",
    "quick": "始终快速退出",
}

SNAPSHOT_SOURCE_LABELS = {
    "closing": "完整关闭",
    "baseline": "启动基线",
    "manual_save": "手动标记",
    "manual": "普通扫描",
    "navigation": "导航扫描",
}

BASELINE_MODE_LABELS = {
    "startup": "本次启动快照",
    "latest_full": "最近完整保存",
}

RUN_MODE_FULL = "full"
RUN_MODE_LOW_MEMORY = "low_memory"
RUN_MODE_LABELS = {
    RUN_MODE_FULL: "全功能模式",
    RUN_MODE_LOW_MEMORY: "低内存模式",
}

MIGRATION_REASON_LABELS = {
    "directory": "目录不作为文件迁移建议",
    "system_root": "系统盘根目录文件",
    "system_or_app_data": "系统或应用数据目录",
    "app_managed_type": "应用管理或系统文件类型",
    "target_same_volume": "目标盘与源卷相同",
    "identity_incomplete": "文件身份或唯一归属不完整",
    "hard_link": "硬链接文件",
    "missing": "文件已不存在",
    "permission_denied": "权限不足",
    "metadata_unavailable": "当前元数据不可读取",
    "reparse_point": "重解析点",
    "cloud_or_offline": "云占位或离线文件",
    "not_regular_file": "不是普通文件",
    "snapshot_mismatch": "当前文件与快照不一致",
}

AUTO_RESCAN_LABELS = {
    "later": "稍后手动补扫（推荐）",
    "now": "立即补扫",
}


class CloseChoiceDialog:
    def __init__(self, parent: tk.Tk) -> None:
        self.choice: str | None = None
        self.remember = False
        self.window = tk.Toplevel(parent)
        self.window.title("关闭监控器")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()

        body = ttk.Frame(self.window, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="如何保存本次监控会话？",
            style="SectionBackground.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            body,
            text="完整保存会扫描监控路径并记录具体变化地址。",
        ).pack(anchor=tk.W, pady=(4, 12))

        choice_var = tk.StringVar(value="full")
        ttk.Radiobutton(
            body,
            text="完整保存（推荐）",
            variable=choice_var,
            value="full",
        ).pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(
            body,
            text="快速退出（只保存磁盘总变化）",
            variable=choice_var,
            value="quick",
        ).pack(anchor=tk.W, pady=2)

        remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="下次不再询问，保持本次选择",
            variable=remember_var,
        ).pack(anchor=tk.W, pady=(12, 10))

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)

        def confirm() -> None:
            self.choice = choice_var.get()
            self.remember = remember_var.get()
            self.window.destroy()

        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(
            side=tk.RIGHT
        )
        ttk.Button(buttons, text="确定", command=confirm).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.window.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.window.winfo_height()) // 2
        self.window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        parent.wait_window(self.window)


class DiskMonitorApp:
    SAMPLE_INTERVAL_MS = 60_000
    TREEMAP_ANIMATION_FRAMES = 12
    MAX_ANIMATED_TREEMAP_ITEMS = 180
    TREND_MAX_POINTS = 2_000
    TREND_GAP_THRESHOLD = timedelta(minutes=5)

    def __init__(
        self,
        root: tk.Tk,
        *,
        storage: Storage | None = None,
        initial_path: str = "C:\\",
        log_path: str | Path | None = None,
        enable_tray: bool = False,
        automation_probe: WindowsSystemProbe | None = None,
    ) -> None:
        self.root = root
        self.storage = storage or Storage()
        self.collect_file_space = (
            self.storage.get_setting("file_space_accounting", "logical")
            == "exact"
        )
        stored_run_mode = self.storage.get_setting("run_mode", RUN_MODE_FULL)
        self.run_mode = (
            stored_run_mode
            if stored_run_mode in RUN_MODE_LABELS
            else RUN_MODE_FULL
        )
        if stored_run_mode not in RUN_MODE_LABELS:
            self.storage.set_setting("run_mode", self.run_mode)
        self.auto_mode_config = AutoModeConfig.load(self.storage)
        self.auto_mode_policy = AutoModePolicy(self.auto_mode_config)
        self.automation_probe = automation_probe or WindowsSystemProbe()
        self.auto_last_observation: AutoObservation | None = None
        self.auto_last_decision: AutoDecision | None = None
        self.automation_status_code = (
            "monitoring" if self.auto_mode_config.enabled else "disabled"
        )
        self.automation_status_reason = (
            "等待首次检测"
            if self.auto_mode_config.enabled
            else "自动模式未启用"
        )
        self.enable_tray = enable_tray
        self.tray_icon: WindowsTrayIcon | None = None
        self.log_path = Path(log_path or self.storage.database_path.parent / "ui.log")
        self.logger = logging.getLogger(f"disk_monitor.ui.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handler = RotatingFileHandler(
            self.log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        self.log_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(message)s"
            )
        )
        self.logger.addHandler(self.log_handler)
        self.root.report_callback_exception = self._report_callback_exception
        requested_path = os.path.abspath(initial_path)
        fallback_path = os.path.abspath("C:\\")
        self.startup_path_was_invalid = not os.path.isdir(requested_path)
        if self.startup_path_was_invalid and os.path.isdir(fallback_path):
            requested_path = fallback_path
        self.messages: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread: threading.Thread | None = None
        self.current_result: ScanResult | None = None
        self.navigation_skeleton: DirectorySkeleton | None = None
        self.rectangle_items: list[tuple[float, float, float, float, ScanItem]] = []
        self.rectangle_canvas_ids: dict[str, int] = {}
        self.hovered_rectangle_path: str | None = None
        self.latest_samples: list[DiskSample] = []
        self.session_boundaries: list[SessionBoundary] = []
        self.current_drive: str | None = None
        self.trend_hours = 24
        self.trend_window_start: datetime | None = None
        self.trend_window_end: datetime | None = None
        self.trend_marker_positions: list[tuple[float, SessionBoundary]] = []
        self.session_id: int | None = None
        self.session_start_sample: DiskSample | None = None
        self.latest_disk_sample: DiskSample | None = None
        self.session_root_path = requested_path
        self.exclude_rules = tuple(
            line.strip()
            for line in self.storage.get_setting("exclude_rules", "").splitlines()
            if line.strip()
        )
        self.session_start_snapshot_id: int | None = None
        self.session_finished = False
        self.closing = False
        self.active_scan_role: str | None = None
        self.baseline_pending = False
        self.close_mode = "full"
        self.change_context: tuple | None = None
        self.default_change_context: tuple | None = None
        self.default_growth_subtitle = ""
        self.default_baseline_info = ""
        self.growth_item_by_id: dict[str, GrowthItem] = {}
        self.pending_scan_note: str | None = None
        self.startup_previous_session = None
        self.automatic_baseline_snapshot_id: int | None = None
        self.automatic_current_snapshot_id: int | None = None
        self.latest_full_snapshot_id: int | None = None
        self.manual_baseline_mode: str | None = None
        self.blind_spot_result: BlindSpotResult | None = None
        self.low_memory_origin: str | None = None
        self.low_memory_started_at: datetime | None = None
        self.low_memory_start_sample: DiskSample | None = None
        self.low_memory_reference_snapshot_id: int | None = None
        self.cold_low_memory_baseline_pending = False
        self.test_low_after_baseline = (
            os.environ.get("DISK_GROWTH_MONITOR_TEST_LOW_AFTER_BASELINE") == "1"
            and os.environ.get("DISK_GROWTH_MONITOR_INSTANCE_NAME", "").startswith(
                "Local\\DiskGrowthMonitorMemory-"
            )
        )
        self.history_cursor: tuple | None = None
        self.history_loaded = False
        self.history_items_by_id: dict[str, SnapshotInfo] = {}
        self.poll_after_id: str | None = None
        self.sample_after_id: str | None = None
        self.baseline_after_id: str | None = None
        self.destroy_after_id: str | None = None
        self.control_after_id: str | None = None
        self.automation_after_id: str | None = None
        self.treemap_animation_after_id: str | None = None
        self.treemap_resize_after_id: str | None = None
        self.display_after_id: str | None = None
        self.display_sync_window: tk.Misc | None = None
        self.settings_window: tk.Toplevel | None = None
        self.migration_window: tk.Toplevel | None = None
        self.snapshot_browser_window: tk.Toplevel | None = None
        self.nav_stack = self._path_chain(requested_path)
        self.nav_cache: dict[str, ScanResult] = {}
        self.nav_invalidated_roots: set[str] = set()

        self.path_var = tk.StringVar(value=requested_path)
        self.status_var = tk.StringVar(value="准备就绪")
        self.mode_status_var = tk.StringVar(value=RUN_MODE_LABELS[self.run_mode])
        self.automation_status_var = tk.StringVar(
            value="自动：监控中" if self.auto_mode_config.enabled else "自动：关闭"
        )
        self.mode_button_var = tk.StringVar(value="切换低内存模式")
        self.total_var = tk.StringVar(value="--")
        self.used_var = tk.StringVar(value="--")
        self.free_var = tk.StringVar(value="--")
        self.change_var = tk.StringVar(value="等待采样")
        self.detail_var = tk.StringVar(value="单击矩形查看详情，双击目录继续分析")
        self.growth_subtitle_var = tk.StringVar(
            value="正在等待启动快照"
        )
        self.change_view_var = tk.StringVar(value="增长")
        self.baseline_mode_var = tk.StringVar(value="自动判定")
        self.baseline_info_var = tk.StringVar(value="基线：正在建立")
        self.comparison_info_var = tk.StringVar(value="当前比较：等待启动扫描")
        self.snapshot_total_var = tk.StringVar(value="--")
        self.runtime_change_var = tk.StringVar(value="等待首次磁盘采样")
        self.blind_spot_var = tk.StringVar(value="等待首次磁盘采样")
        self.low_memory_change_var = tk.StringVar(value="尚未进入低内存模式")
        self.history_status_var = tk.StringVar(value="请选择两条同路径快照进行对比")
        self.history_path_filter_var = tk.StringVar()
        self.history_source_filter_var = tk.StringVar(value="全部来源")
        self.history_after_filter_var = tk.StringVar()
        self.history_before_filter_var = tk.StringVar()
        self.trend_range_var = tk.StringVar(value="24 小时")
        self.trend_title_var = tk.StringVar(value="最近 24 小时已用空间")
        self.trend_hover_var = tk.StringVar(value="悬停趋势线查看精确数值")
        self.growth_hover_var = tk.StringVar(value="悬停项目查看完整路径")
        self.context_status_var = tk.StringVar(
            value="基线：正在建立 · 扫描：尚无数据 · 盲区：等待采样"
        )
        self.last_scan_summary = "尚无数据"
        self.treemap_placeholder_text = "点击“重新扫描当前目录”生成空间分布图"

        self.display_metrics: DisplayMetrics = sync_tk_scaling(self.root)
        self.ui_scale = self.display_metrics.dpi / 96
        self._configure_fonts()
        self._configure_window()
        self._configure_styles()
        self._build_ui()
        self._apply_run_mode_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._finalize_on_process_exit)
        self.logger.info(
            "app_started version=%s initial_path=%s run_mode=%s",
            __version__,
            requested_path,
            self.run_mode,
        )
        self.logger.info(
            "display_config dpi=%s tk_scaling=%.4f changed=%s",
            self.display_metrics.dpi,
            self.display_metrics.target_scaling,
            self.display_metrics.changed,
        )
        self.poll_after_id = self.root.after(100, self._poll_messages)
        self.sample_after_id = self.root.after(200, self._sample_now)
        self.agent_controller = GuiAgentController(self)
        self.control_bridge: GuiControlBridge | None = None
        try:
            self.control_bridge = GuiControlBridge(
                self.storage.database_path.parent,
                logger=self.logger,
            )
            self.control_bridge.start()
            self.control_after_id = self.root.after(
                50, self._poll_control_requests
            )
        except Exception:
            self.logger.exception("control_server_start_failed")
            self.status_var.set(
                "程序已启动，但 Agent 本地控制服务不可用；详情见 ui.log"
            )
        self._start_tray()
        self.automation_after_id = self.root.after(
            1_500, self._poll_automation
        )

    def _poll_control_requests(self) -> None:
        self.control_after_id = None
        try:
            if self.control_bridge is not None:
                self.control_bridge.drain(self.agent_controller.handle)
        except Exception:
            self.logger.exception("control_bridge_poll_failed")
        finally:
            try:
                if self.root.winfo_exists() and not self.closing:
                    self.control_after_id = self.root.after(
                        50, self._poll_control_requests
                    )
            except tk.TclError:
                pass

    def _start_tray(self) -> None:
        if not self.enable_tray:
            return
        try:
            self.tray_icon = WindowsTrayIcon(
                self.app_icon_path,
                lambda command: self.messages.put(("tray_command", command)),
                logger=self.logger,
            )
            self.tray_icon.start()
            self._update_tray_state()
        except Exception:
            self.logger.exception("tray_start_failed")
            self.tray_icon = None
            self.status_var.set(
                "程序已启动，但 Windows 托盘不可用；详情见 ui.log"
            )

    def _window_is_visible(self) -> bool:
        try:
            return self.root.state() not in {"withdrawn", "iconic"}
        except tk.TclError:
            return False

    def _update_tray_state(self) -> None:
        if self.tray_icon is None:
            return
        scan_busy = self.active_scan_role is not None or bool(
            self.scan_thread and self.scan_thread.is_alive()
        )
        self.tray_icon.update_state(
            TrayState(
                RUN_MODE_LABELS[self.run_mode],
                self.automation_status_var.get(),
                self._window_is_visible(),
                scan_busy,
            )
        )

    def _notify_tray(self, title: str, message: str) -> None:
        if self.tray_icon is not None:
            self.tray_icon.notify(title, message)

    def _on_window_visibility_changed(self, _event: tk.Event | None = None) -> None:
        if _event is None or _event.widget is self.root:
            self._update_tray_state()

    def _handle_tray_command(self, command: str) -> None:
        if self.closing:
            return
        if command == "show":
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        elif command == "hide":
            self.root.withdraw()
        elif command == "mode_low":
            if self.session_id is None or self.session_start_sample is None:
                self._notify_tray("尚未就绪", "请等待首次磁盘采样完成")
            elif self.active_scan_role is not None or (
                self.scan_thread and self.scan_thread.is_alive()
            ):
                self._notify_tray("扫描进行中", "扫描结束后才能切换低内存模式")
            elif self.run_mode != RUN_MODE_LOW_MEMORY:
                self._enter_low_memory_mode()
                self._note_manual_mode_change("tray")
        elif command == "mode_full":
            if self.session_id is None or self.session_start_sample is None:
                self._notify_tray("尚未就绪", "请等待首次磁盘采样完成")
            elif self.run_mode != RUN_MODE_FULL:
                self._leave_low_memory_mode(should_scan=False)
                self._note_manual_mode_change("tray")
        elif command == "rescan":
            if self.run_mode == RUN_MODE_FULL:
                self._refresh_current_path()
        elif command == "settings":
            self._open_settings(from_tray=True)
        elif command == "exit_quick":
            self._request_controlled_close("quick")
        elif command == "exit_full":
            self._request_controlled_close("full")
        else:
            self.logger.warning("unknown_tray_command command=%s", command)
        self._update_tray_state()

    def _poll_automation(self) -> None:
        self.automation_after_id = None
        try:
            self._run_automation_check()
        except Exception as error:
            self.logger.exception("automation_poll_failed")
            self._set_automation_status("error", str(error))
        finally:
            try:
                if self.root.winfo_exists() and not self.closing:
                    self.automation_after_id = self.root.after(
                        self.auto_mode_config.poll_interval_ms,
                        self._poll_automation,
                    )
            except tk.TclError:
                pass

    def _run_automation_check(
        self, observation: AutoObservation | None = None
    ) -> AutoDecision | None:
        if not self.auto_mode_config.enabled:
            decision = self.auto_mode_policy.evaluate(
                observation or AutoObservation(0),
                run_mode=self.run_mode,
                scan_busy=False,
            )
            self.auto_last_decision = decision
            self._set_automation_status(decision.status, decision.reason)
            return decision
        if self.session_id is None or self.session_start_sample is None:
            self._set_automation_status("initializing", "等待首次磁盘采样")
            return None
        current_observation = observation or self.automation_probe.observe(
            self.auto_mode_config.process_names
        )
        self.auto_last_observation = current_observation
        scan_busy = self.active_scan_role is not None or bool(
            self.scan_thread and self.scan_thread.is_alive()
        )
        decision = self.auto_mode_policy.evaluate(
            current_observation,
            run_mode=self.run_mode,
            scan_busy=scan_busy,
        )
        self.auto_last_decision = decision
        self._set_automation_status(decision.status, decision.reason)
        if decision.action == "enter_low":
            self._enter_low_memory_mode()
            self.auto_mode_policy.mark_auto_entered()
            self._set_automation_status("auto_low", decision.reason)
            self.status_var.set(f"已自动切换低内存模式：{decision.reason}")
            self._notify_tray("已自动切换低内存模式", decision.reason)
            self.logger.info(
                "automation_mode_changed mode=low_memory reason=%s",
                decision.reason,
            )
        elif decision.action == "leave_low":
            should_scan = self.auto_mode_config.resume_rescan == "now"
            change_text = self._low_memory_change_text()
            if self._leave_low_memory_mode(should_scan=should_scan):
                self.auto_mode_policy.mark_auto_left()
                self._set_automation_status("monitoring", decision.reason)
                suffix = "正在补扫当前路径" if should_scan else "可稍后手动补扫"
                self.status_var.set(
                    f"已自动恢复全功能模式；{change_text}；{suffix}"
                )
                self._notify_tray(
                    "已自动恢复全功能模式",
                    f"{change_text}；{suffix}",
                )
                self.logger.info(
                    "automation_mode_changed mode=full rescan=%s",
                    should_scan,
                )
        self._update_tray_state()
        return decision

    def _low_memory_change_text(self) -> str:
        if self.latest_disk_sample is None or self.low_memory_start_sample is None:
            return "低内存期间变化尚无可用采样"
        change = (
            self.latest_disk_sample.used_bytes
            - self.low_memory_start_sample.used_bytes
        )
        sign = "+" if change > 0 else ""
        return f"低内存期间磁盘变化 {sign}{format_bytes(change)}"

    def _set_automation_status(self, status: str, reason: str) -> None:
        labels = {
            "disabled": "自动：关闭",
            "initializing": "自动：初始化",
            "monitoring": "自动：监控中",
            "detecting": "自动：确认触发",
            "waiting_for_scan": "自动：等待扫描",
            "switching": "自动：切换中",
            "auto_low": "自动：低内存",
            "recovering": "自动：等待恢复",
            "manual_override": "自动：人工优先",
            "manual_low": "自动：人工低内存",
            "error": "自动：检测异常",
        }
        self.automation_status_code = status
        self.automation_status_reason = reason
        label = labels.get(status, "自动：未知")
        if self.auto_last_observation is not None and status != "disabled":
            label += f" · 内存 {self.auto_last_observation.memory_percent}%"
        self.automation_status_var.set(label)
        self._update_tray_state()

    def _note_manual_mode_change(self, source: str) -> None:
        self.auto_mode_policy.note_manual_mode_change()
        self.logger.info("automation_manual_override source=%s", source)
        if self.auto_mode_config.enabled:
            self._set_automation_status(
                "manual_override"
                if self.auto_mode_policy.manual_hold
                else "monitoring",
                "人工模式选择优先",
            )

    def _automation_data(self) -> dict[str, object]:
        decision = self.auto_last_decision
        return {
            "config": self.auto_mode_config.to_dict(),
            "status": self.automation_status_code,
            "reason": self.automation_status_reason,
            "owns_low_mode": self.auto_mode_policy.owns_low_mode,
            "manual_hold": self.auto_mode_policy.manual_hold,
            "triggers": list(decision.triggers) if decision else [],
            "observation": (
                self.auto_last_observation.to_dict()
                if self.auto_last_observation is not None
                else None
            ),
        }

    def _apply_automation_config(
        self, config: AutoModeConfig, *, persist: bool = True
    ) -> None:
        config.validate()
        if persist:
            config.save(self.storage)
        self.auto_mode_config = config
        self.auto_mode_policy.update_config(config)
        self.auto_last_decision = None
        self._set_automation_status(
            "monitoring" if config.enabled else "disabled",
            "等待下次检测" if config.enabled else "自动模式未启用",
        )
        if self.automation_after_id is not None:
            try:
                self.root.after_cancel(self.automation_after_id)
            except tk.TclError:
                pass
        self.automation_after_id = self.root.after(250, self._poll_automation)

    def _update_automation_from_control(
        self, changes: dict[str, object]
    ) -> dict[str, object]:
        current = self.auto_mode_config

        def boolean_value(key: str, default: bool) -> bool:
            value = changes.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值")
            return value

        process_value = changes.get("process_names", current.process_names)
        if not isinstance(process_value, (str, list, tuple)):
            raise ValueError("process_names 必须是字符串或字符串列表")
        if isinstance(process_value, (list, tuple)) and not all(
            isinstance(item, str) for item in process_value
        ):
            raise ValueError("process_names 列表只能包含字符串")

        def integer_value(key: str, default: int) -> int:
            value = changes.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{key} 必须是整数")
            return value

        resume_rescan = changes.get("resume_rescan", current.resume_rescan)
        if not isinstance(resume_rescan, str):
            raise ValueError("resume_rescan 必须是字符串")
        config = AutoModeConfig(
            enabled=boolean_value("enabled", current.enabled),
            process_names=normalize_process_names(
                tuple(process_value)
                if isinstance(process_value, (list, tuple))
                else process_value
            ),
            memory_pressure_enabled=boolean_value(
                "memory_pressure_enabled", current.memory_pressure_enabled
            ),
            high_percent=integer_value("high_percent", current.high_percent),
            low_percent=integer_value("low_percent", current.low_percent),
            resume_rescan=resume_rescan,
        ).validate()
        self._apply_automation_config(config)
        return self._automation_data()

    def _report_callback_exception(
        self, exception_type: type[BaseException], exception: BaseException, traceback
    ) -> None:
        self.logger.error(
            "tk_callback_failed",
            exc_info=(exception_type, exception, traceback),
        )
        try:
            self.status_var.set(
                f"界面操作失败：{exception}；扫描数据不会被删除，详情见 ui.log"
            )
        except (AttributeError, tk.TclError):
            pass

    def _record_ui_error(self, context: str, error: BaseException) -> None:
        self.logger.exception("ui_operation_failed context=%s", context)
        self.status_var.set(
            f"{context}失败：{error}；已保存的数据不受影响，详情见 ui.log"
        )

    def _configure_window(self) -> None:
        self.root.title("C 盘空间增长监控器")
        cursor = cursor_work_area()
        if cursor is None:
            work_left = 0
            work_top = 0
            work_width = self.root.winfo_screenwidth()
            work_height = self.root.winfo_screenheight()
        else:
            _cursor_x, _cursor_y, work_area = cursor
            work_left = work_area.left
            work_top = work_area.top
            work_width = work_area.right - work_area.left
            work_height = work_area.bottom - work_area.top
        edge_margin = self._px(24)
        width = max(1, min(self._px(1180), work_width - edge_margin))
        height = max(1, min(self._px(780), work_height - edge_margin))
        minimum_width = min(self._px(900), width)
        minimum_height = min(self._px(720), height)
        x = work_left + max(0, (work_width - width) // 2)
        y = work_top + max(0, (work_height - height) // 2)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.minsize(minimum_width, minimum_height)
        self.root.configure(bg=COLORS["background"])
        bundle_root = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
        )
        icon_path = bundle_root / "assets" / "app.ico"
        self.app_icon_path = icon_path
        if icon_path.exists():
            self.root.iconbitmap(default=str(icon_path))
        self.root.bind("<Map>", self._on_window_visibility_changed, add="+")
        self.root.bind("<Unmap>", self._on_window_visibility_changed, add="+")
        self.root.bind("<Configure>", self._schedule_display_sync, add="+")

    def _configure_dialog_window(
        self,
        window: tk.Toplevel,
        *,
        width: int,
        height: int,
        minimum_width: int,
        minimum_height: int,
    ) -> None:
        cursor = cursor_work_area()
        if cursor is None:
            work_width = window.winfo_screenwidth()
            work_height = window.winfo_screenheight()
        else:
            _cursor_x, _cursor_y, work_area = cursor
            work_width = work_area.right - work_area.left
            work_height = work_area.bottom - work_area.top
        edge_margin = self._px(24)
        actual_width = max(1, min(self._px(width), work_width - edge_margin))
        actual_height = max(1, min(self._px(height), work_height - edge_margin))
        window.geometry(f"{actual_width}x{actual_height}")
        window.minsize(
            min(self._px(minimum_width), actual_width),
            min(self._px(minimum_height), actual_height),
        )

    def _px(self, value: int) -> int:
        return max(1, round(value * self.ui_scale))

    def _configure_fonts(self) -> None:
        family = "Microsoft YaHei UI"
        for name, size, weight in (
            ("TkDefaultFont", 10, "normal"),
            ("TkTextFont", 10, "normal"),
            ("TkMenuFont", 10, "normal"),
            ("TkCaptionFont", 10, "normal"),
            ("TkSmallCaptionFont", 9, "normal"),
            ("TkHeadingFont", 10, "bold"),
            ("TkIconFont", 10, "normal"),
            ("TkTooltipFont", 9, "normal"),
        ):
            try:
                tkfont.nametofont(name, root=self.root).configure(
                    family=family,
                    size=size,
                    weight=weight,
                )
            except tk.TclError:
                continue
        self.fonts = {
            "title": tkfont.Font(
                root=self.root, family=family, size=18, weight="bold"
            ),
            "dialog_title": tkfont.Font(
                root=self.root, family=family, size=15, weight="bold"
            ),
            "section": tkfont.Font(
                root=self.root, family=family, size=11, weight="bold"
            ),
            "subheading": tkfont.Font(
                root=self.root, family=family, size=10, weight="bold"
            ),
            "body": tkfont.Font(root=self.root, family=family, size=10),
            "body_emphasis": tkfont.Font(
                root=self.root, family=family, size=11
            ),
            "caption": tkfont.Font(root=self.root, family=family, size=9),
            "caption_bold": tkfont.Font(
                root=self.root, family=family, size=9, weight="bold"
            ),
            "tiny": tkfont.Font(root=self.root, family=family, size=8),
            "tiny_bold": tkfont.Font(
                root=self.root, family=family, size=8, weight="bold"
            ),
            "metric": tkfont.Font(
                root=self.root, family=family, size=16, weight="bold"
            ),
        }

    def _schedule_display_sync(self, event: tk.Event | None = None) -> None:
        if event is not None:
            if event.widget not in {self.root, self.settings_window}:
                return
            self.display_sync_window = event.widget
        if self.display_after_id is not None:
            return
        try:
            self.display_after_id = self.root.after(150, self._sync_display_scaling)
        except tk.TclError:
            self.display_after_id = None

    def _sync_display_scaling(self) -> None:
        self.display_after_id = None
        window = self.display_sync_window or self.root
        self.display_sync_window = None
        try:
            metrics = sync_tk_scaling(window)
        except tk.TclError:
            return
        if not metrics.changed:
            self.display_metrics = metrics
            return
        previous = self.display_metrics
        self.display_metrics = metrics
        self.ui_scale = metrics.dpi / 96
        self._update_scaled_style_metrics()
        self.logger.info(
            "display_dpi_changed old_dpi=%s new_dpi=%s tk_scaling=%.4f",
            previous.dpi,
            metrics.dpi,
            metrics.target_scaling,
        )

    def _update_scaled_style_metrics(self) -> None:
        row_height = max(
            self._px(27),
            int(self.fonts["caption"].metrics("linespace")) + self._px(5),
        )
        self.style.configure("Treeview", rowheight=row_height)

    def _configure_styles(self) -> None:
        style = ttk.Style()
        self.style = style
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure(
            "TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=self.fonts["body"],
        )
        style.configure(
            "TButton",
            padding=(self._px(10), self._px(5)),
            font=self.fonts["body"],
        )
        style.configure(
            "TNotebook.Tab",
            padding=(self._px(14), self._px(7)),
            font=self.fonts["body"],
        )
        style.configure(
            "Panel.TCheckbutton",
            background=COLORS["panel"],
            font=self.fonts["body"],
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=self.fonts["title"],
        )
        style.configure(
            "DialogTitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=self.fonts["dialog_title"],
        )
        style.configure(
            "Section.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=self.fonts["section"],
        )
        style.configure(
            "SectionBackground.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=self.fonts["section"],
        )
        style.configure(
            "PanelMuted.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=self.fonts["caption"],
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["muted"],
            font=self.fonts["caption"],
        )
        style.configure(
            "MetricName.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=self.fonts["caption"],
        )
        style.configure(
            "MetricValue.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=self.fonts["metric"],
        )
        style.configure(
            "ChangeIncrease.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["positive"],
            font=self.fonts["metric"],
        )
        style.configure(
            "ChangeDecrease.TLabel",
            background=COLORS["panel"],
            foreground="#15803d",
            font=self.fonts["metric"],
        )
        style.configure("Accent.TButton", font=self.fonts["caption_bold"])
        style.configure("Treeview", font=self.fonts["caption"])
        style.configure("Treeview.Heading", font=self.fonts["caption_bold"])
        self._update_scaled_style_metrics()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill=tk.X)
        ttk.Label(title_row, text="C 盘空间增长监控器", style="Title.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            title_row,
            text="持续记录 · 快照对比 · 增长溯源",
            style="Subtitle.TLabel",
        ).pack(side=tk.LEFT, padx=(14, 0), pady=(8, 0))
        self.mode_toggle_button = ttk.Button(
            title_row,
            textvariable=self.mode_button_var,
            command=self._toggle_run_mode,
        )
        self.mode_toggle_button.pack(side=tk.RIGHT)
        ttk.Label(
            title_row,
            textvariable=self.mode_status_var,
            style="Subtitle.TLabel",
        ).pack(side=tk.RIGHT, padx=(0, 10), pady=(4, 0))
        ttk.Label(
            title_row,
            textvariable=self.automation_status_var,
            style="Subtitle.TLabel",
        ).pack(side=tk.RIGHT, padx=(0, 10), pady=(4, 0))

        metrics = ttk.Frame(outer)
        metrics.pack(fill=tk.X, pady=(16, 12))
        for title, variable in (
            ("磁盘总容量", self.total_var),
            ("已使用", self.used_var),
            ("可用空间", self.free_var),
            ("本次启动后变化（磁盘口径）", self.change_var),
        ):
            card = ttk.Frame(metrics, style="Panel.TFrame", padding=(16, 12))
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
            ttk.Label(card, text=title, style="MetricName.TLabel").pack(anchor=tk.W)
            value_row = ttk.Frame(card, style="Panel.TFrame")
            value_row.pack(anchor=tk.W, pady=(4, 0))
            value_label = ttk.Label(
                value_row, textvariable=variable, style="MetricValue.TLabel"
            )
            value_label.pack(side=tk.LEFT)
            if variable is self.change_var:
                self.change_label = value_label
                ttk.Label(
                    value_row,
                    text="相对启动时",
                    style="MetricName.TLabel",
                ).pack(side=tk.LEFT, padx=(6, 0), pady=(5, 0))

        self.notebook = ttk.Notebook(outer)
        self.distribution_tab = ttk.Frame(self.notebook, padding=10)
        self.changes_tab = ttk.Frame(self.notebook, padding=10)
        self.history_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.distribution_tab, text="① 空间分布")
        self.notebook.add(self.changes_tab, text="② 空间变化")
        self.notebook.add(self.history_tab, text="③ 快照历史")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        controls = ttk.Frame(
            self.distribution_tab, style="Panel.TFrame", padding=10
        )
        controls.pack(fill=tk.X, pady=(0, 8))
        self.up_button = ttk.Button(
            controls, text="↑ 上一级", command=self._go_up, state=tk.DISABLED
        )
        self.up_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(controls, text="扫描路径：", background=COLORS["panel"]).pack(
            side=tk.LEFT
        )
        self.path_entry = ttk.Entry(controls, textvariable=self.path_var)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        self.choose_directory_button = ttk.Button(
            controls, text="选择目录", command=self._choose_directory
        )
        self.choose_directory_button.pack(
            side=tk.LEFT, padx=(0, 8)
        )
        self.scan_button = ttk.Button(
            controls,
            text="重新扫描当前目录",
            command=self._refresh_current_path,
            style="Accent.TButton",
        )
        self.scan_button.pack(side=tk.LEFT, padx=(0, 8))
        self.cancel_button = ttk.Button(
            controls, text="取消扫描", command=self._cancel_scan, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT)
        self.save_snapshot_button = ttk.Button(
            controls, text="保存快照", command=self._save_marked_snapshot
        )
        self.save_snapshot_button.pack(side=tk.LEFT, padx=(8, 0))
        self.migration_advice_button = ttk.Button(
            controls,
            text="迁移建议",
            command=self._open_migration_advice,
            state=tk.DISABLED,
        )
        self.migration_advice_button.pack(side=tk.LEFT, padx=(8, 0))
        self.search_filter_button = ttk.Button(
            controls,
            text="查找/筛选",
            command=self._open_current_snapshot_browser,
            state=tk.DISABLED,
        )
        self.search_filter_button.pack(side=tk.LEFT, padx=(8, 0))
        self.settings_button = ttk.Button(
            controls, text="设置", command=self._open_settings
        )
        self.settings_button.pack(
            side=tk.LEFT, padx=(8, 0)
        )

        self.breadcrumb_frame = ttk.Frame(self.distribution_tab)
        self.breadcrumb_frame.pack(fill=tk.X, pady=(0, 8))
        self._refresh_breadcrumbs()

        map_panel = ttk.Frame(
            self.distribution_tab, style="Panel.TFrame", padding=10
        )
        map_panel.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            map_panel,
            text="空间分布",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=self.fonts["section"],
        ).pack(anchor=tk.W)
        ttk.Label(
            map_panel,
            textvariable=self.detail_var,
            background=COLORS["panel"],
            foreground=COLORS["muted"],
        ).pack(anchor=tk.W, pady=(2, 8))
        self.map_canvas = tk.Canvas(
            map_panel,
            background="#f8fafc",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.map_canvas.pack(fill=tk.BOTH, expand=True)
        self.map_canvas.bind("<Configure>", self._schedule_treemap_resize)
        self.map_canvas.bind("<Button-1>", self._select_rectangle)
        self.map_canvas.bind("<Double-Button-1>", self._open_rectangle)
        self.map_canvas.bind("<Motion>", self._hover_rectangle)
        self.map_canvas.bind("<Leave>", self._leave_treemap)

        trend_panel = ttk.Frame(
            self.distribution_tab, style="Panel.TFrame", padding=10
        )
        trend_panel.pack(
            fill=tk.X,
            side=tk.BOTTOM,
            pady=(8, 0),
            before=map_panel,
        )
        trend_header = ttk.Frame(trend_panel, style="Panel.TFrame")
        trend_header.pack(fill=tk.X)
        ttk.Label(
            trend_header,
            textvariable=self.trend_title_var,
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=self.fonts["subheading"],
        ).pack(side=tk.LEFT)
        ttk.Label(
            trend_header,
            textvariable=self.trend_hover_var,
            background=COLORS["panel"],
            foreground=COLORS["muted"],
        ).pack(side=tk.RIGHT, padx=(8, 0))
        trend_range = ttk.Combobox(
            trend_header,
            textvariable=self.trend_range_var,
            values=("24 小时", "7 天", "30 天"),
            state="readonly",
            width=8,
        )
        trend_range.pack(side=tk.RIGHT)
        trend_range.bind("<<ComboboxSelected>>", self._change_trend_range)
        self.trend_canvas = tk.Canvas(
            trend_panel,
            height=85,
            background=COLORS["panel"],
            highlightthickness=0,
        )
        self.trend_canvas.pack(fill=tk.X)
        self.trend_canvas.bind("<Configure>", lambda _event: self._draw_trend())
        self.trend_canvas.bind("<Motion>", self._hover_trend)
        self.trend_canvas.bind("<Leave>", self._leave_trend)

        growth_header = ttk.Frame(self.changes_tab, style="Panel.TFrame")
        growth_header.pack(fill=tk.X)
        ttk.Label(
            growth_header,
            text="空间变化来源（文件口径）",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=self.fonts["section"],
        ).pack(side=tk.LEFT)
        self.change_view_selector = ttk.Combobox(
            growth_header,
            textvariable=self.change_view_var,
            values=("增长", "减少"),
            state="readonly",
            width=6,
        )
        self.change_view_selector.pack(side=tk.RIGHT)
        self.change_view_selector.bind(
            "<<ComboboxSelected>>", self._refresh_change_view
        )
        self.restore_baseline_button = ttk.Button(
            growth_header,
            text="恢复默认基线",
            command=self._restore_default_baseline,
        )
        self.restore_baseline_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.baseline_mode_selector = ttk.Combobox(
            growth_header,
            textvariable=self.baseline_mode_var,
            state=tk.DISABLED,
            width=14,
        )
        self.baseline_mode_selector.pack(side=tk.RIGHT, padx=(0, 6))
        self.baseline_mode_selector.bind(
            "<<ComboboxSelected>>", self._select_baseline_mode
        )
        ttk.Label(
            growth_header,
            text="检测基线：",
            foreground=COLORS["muted"],
        ).pack(side=tk.RIGHT)

        ttk.Label(
            self.changes_tab,
            textvariable=self.baseline_info_var,
            foreground=COLORS["text"],
            font=self.fonts["caption_bold"],
        ).pack(anchor=tk.W, pady=(8, 2))
        ttk.Label(
            self.changes_tab,
            textvariable=self.comparison_info_var,
            foreground=COLORS["muted"],
        ).pack(anchor=tk.W, pady=(0, 8))

        change_summary = ttk.Frame(
            self.changes_tab, style="Panel.TFrame", padding=(14, 8)
        )
        change_summary.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            change_summary,
            text="文件快照变化（文件逻辑大小）",
            style="MetricName.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 14), pady=3)
        self.snapshot_total_label = tk.Label(
            change_summary,
            textvariable=self.snapshot_total_var,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=self.fonts["section"],
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self.snapshot_total_label.grid(row=0, column=1, sticky=tk.EW, pady=3)

        ttk.Label(
            change_summary,
            text="本次运行期间磁盘变化（磁盘已用容量）",
            style="MetricName.TLabel",
        ).grid(row=1, column=0, sticky=tk.W, padx=(0, 14), pady=3)
        self.runtime_change_label = tk.Label(
            change_summary,
            textvariable=self.runtime_change_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=self.fonts["subheading"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=570,
        )
        self.runtime_change_label.grid(row=1, column=1, sticky=tk.EW, pady=3)

        ttk.Label(
            change_summary,
            text="未监控期间磁盘变化（磁盘已用容量）",
            style="MetricName.TLabel",
        ).grid(row=2, column=0, sticky=tk.W, padx=(0, 14), pady=3)
        self.blind_spot_label = tk.Label(
            change_summary,
            textvariable=self.blind_spot_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=self.fonts["subheading"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=570,
        )
        self.blind_spot_label.grid(row=2, column=1, sticky=tk.EW, pady=3)

        ttk.Label(
            change_summary,
            text="本次启动后的低内存期间变化（磁盘已用容量）",
            style="MetricName.TLabel",
        ).grid(row=3, column=0, sticky=tk.W, padx=(0, 14), pady=3)
        self.low_memory_change_label = tk.Label(
            change_summary,
            textvariable=self.low_memory_change_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=self.fonts["subheading"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=570,
        )
        self.low_memory_change_label.grid(row=3, column=1, sticky=tk.EW, pady=3)
        change_summary.grid_columnconfigure(1, weight=1)

        ttk.Label(
            self.changes_tab,
            textvariable=self.growth_subtitle_var,
            background=COLORS["panel"],
            foreground=COLORS["muted"],
        ).pack(anchor=tk.W, pady=(2, 8))
        self.growth_hover_label = ttk.Label(
            self.changes_tab,
            textvariable=self.growth_hover_var,
            foreground=COLORS["muted"],
            wraplength=800,
        )
        self.growth_hover_label.pack(fill=tk.X, anchor=tk.W, pady=(0, 6))

        columns = ("size", "change")
        self.growth_tree = ttk.Treeview(
            self.changes_tab,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.growth_tree.heading("#0", text="项目")
        self.growth_tree.heading("size", text="当前")
        self.growth_tree.heading("change", text="增长")
        self.growth_tree.column("#0", width=210, anchor=tk.W)
        self.growth_tree.column("size", width=85, anchor=tk.E)
        self.growth_tree.column("change", width=85, anchor=tk.E)
        scrollbar = ttk.Scrollbar(
            self.changes_tab, orient=tk.VERTICAL, command=self.growth_tree.yview
        )
        self.growth_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.growth_tree.pack(fill=tk.BOTH, expand=True)
        self.growth_tree.bind("<<TreeviewSelect>>", self._select_growth_item)
        self.growth_tree.bind("<Double-Button-1>", self._open_growth_item)
        self.growth_tree.bind("<Motion>", self._hover_growth_item)
        self.growth_tree.bind("<Leave>", self._leave_growth_tree)
        self.growth_tree.bind("<Configure>", self._resize_growth_columns)

        history_header = ttk.Frame(self.history_tab)
        history_header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            history_header,
            text="快照历史",
            foreground=COLORS["text"],
            font=self.fonts["section"],
        ).pack(side=tk.LEFT)
        self.compare_history_button = ttk.Button(
            history_header,
            text="对比所选两项",
            command=self._compare_selected_snapshots,
            state=tk.DISABLED,
        )
        self.compare_history_button.pack(side=tk.RIGHT)
        self.view_history_button = ttk.Button(
            history_header,
            text="查看/查找所选项",
            command=self._open_selected_snapshot_browser,
            state=tk.DISABLED,
        )
        self.view_history_button.pack(side=tk.RIGHT, padx=(0, 8))
        history_filters = ttk.Frame(self.history_tab, padding=(0, 0, 0, 8))
        history_filters.pack(fill=tk.X)
        ttk.Label(history_filters, text="根路径").pack(side=tk.LEFT)
        ttk.Entry(
            history_filters, textvariable=self.history_path_filter_var, width=28
        ).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(history_filters, text="来源").pack(side=tk.LEFT)
        ttk.Combobox(
            history_filters,
            textvariable=self.history_source_filter_var,
            values=("全部来源", *SNAPSHOT_SOURCE_LABELS.values()),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(history_filters, text="从").pack(side=tk.LEFT)
        ttk.Entry(
            history_filters, textvariable=self.history_after_filter_var, width=12
        ).pack(side=tk.LEFT, padx=(5, 6))
        ttk.Label(history_filters, text="到").pack(side=tk.LEFT)
        ttk.Entry(
            history_filters, textvariable=self.history_before_filter_var, width=12
        ).pack(side=tk.LEFT, padx=(5, 8))
        ttk.Button(
            history_filters,
            text="应用筛选",
            command=lambda: self._load_snapshot_history(reset=True),
        ).pack(side=tk.LEFT)
        ttk.Label(
            history_filters,
            text="日期格式 YYYY-MM-DD",
            foreground=COLORS["muted"],
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            self.history_tab,
            textvariable=self.history_status_var,
            foreground=COLORS["muted"],
        ).pack(anchor=tk.W, pady=(0, 8))

        history_columns = ("time", "path", "size", "source", "note")
        self.history_tree = ttk.Treeview(
            self.history_tab,
            columns=history_columns,
            show="headings",
            selectmode="extended",
        )
        for column, title, width, anchor in (
            ("time", "时间", 145, tk.W),
            ("path", "路径", 330, tk.W),
            ("size", "大小", 90, tk.E),
            ("source", "来源", 90, tk.CENTER),
            ("note", "备注", 180, tk.W),
        ):
            self.history_tree.heading(column, text=title)
            self.history_tree.column(column, width=width, anchor=anchor)
        self.history_tree.tag_configure("manual", background="#fff7d6")
        history_scrollbar = ttk.Scrollbar(
            self.history_tab,
            orient=tk.VERTICAL,
            command=self.history_tree.yview,
        )
        self.history_tree.configure(yscrollcommand=history_scrollbar.set)
        history_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_tree.pack(fill=tk.BOTH, expand=True)
        self.history_tree.bind(
            "<<TreeviewSelect>>", self._update_history_selection
        )
        self.history_tree.bind("<Button-1>", self._toggle_history_selection)
        self.load_more_button = ttk.Button(
            self.history_tab,
            text="加载更多",
            command=self._load_more_snapshots,
            state=tk.DISABLED,
        )
        self.load_more_button.pack(anchor=tk.CENTER, pady=(8, 0))

        footer = ttk.Frame(outer)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=180)
        self.progress.pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.status_var, style="Subtitle.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )
        self.context_status_label = ttk.Label(
            outer,
            textvariable=self.context_status_var,
            style="Subtitle.TLabel",
            justify=tk.LEFT,
        )
        self.context_status_label.pack(
            side=tk.BOTTOM, fill=tk.X, pady=(3, 0)
        )
        outer.bind(
            "<Configure>",
            lambda event: self.context_status_label.configure(
                wraplength=max(event.width - 12, 400)
            ),
        )
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.notebook.pack(fill=tk.BOTH, expand=True)

    def _choose_directory(self) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self.status_var.set("低内存模式不执行目录扫描")
            return
        selected = filedialog.askdirectory(
            title="选择要扫描的目录", initialdir=self.path_var.get()
        )
        if selected:
            self.path_var.set(selected)

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    @classmethod
    def _path_chain(cls, path: str) -> list[str]:
        normalized = cls._normalize_path(path)
        drive, tail = os.path.splitdrive(normalized)
        if drive:
            current = drive + os.sep
            chain = [current]
        else:
            current = os.sep
            chain = [current]
        for part in tail.strip("\\/").split(os.sep):
            if part:
                current = os.path.join(current, part)
                chain.append(current)
        return chain

    @classmethod
    def _path_is_within(cls, path: str, parent: str) -> bool:
        try:
            normalized_path = cls._normalize_path(path)
            normalized_parent = cls._normalize_path(parent)
            return os.path.commonpath((normalized_path, normalized_parent)) == normalized_parent
        except ValueError:
            return False

    def _refresh_current_path(self) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self.status_var.set("低内存模式不执行目录扫描；请先切回全功能模式")
            return
        path = self.path_var.get().strip()
        if not path:
            messagebox.showerror("无法扫描", "请输入一个存在的目录路径。")
            return
        normalized = self._normalize_path(path)
        self.nav_stack = self._path_chain(normalized)
        self._refresh_breadcrumbs()
        self._invalidate_navigation_cache(normalized)
        self._start_scan(role="manual", path=normalized)

    def _save_marked_snapshot(self) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self.status_var.set("低内存模式不建立文件快照；请先切回全功能模式")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showinfo("扫描进行中", "请等待当前扫描结束后再保存快照。")
            return
        note = simpledialog.askstring(
            "保存手动快照",
            "请输入快照备注（必填）：",
            parent=self.root,
        )
        if note is None:
            return
        note = note.strip()
        if not note:
            messagebox.showwarning("需要备注", "手动快照必须填写备注。")
            return
        raw_path = self.path_var.get().strip()
        if not raw_path or not os.path.isdir(raw_path):
            messagebox.showerror("无法保存", "请输入一个存在的目录路径。")
            return
        path = self._normalize_path(raw_path)
        self.nav_stack = self._path_chain(path)
        self._refresh_breadcrumbs()
        self._invalidate_navigation_cache(path)
        self.pending_scan_note = note
        self._start_scan(role="manual_save", path=path)

    def _go_up(self) -> None:
        if len(self.nav_stack) <= 1:
            return
        self.nav_stack.pop()
        self._navigate_to(self.nav_stack[-1], update_stack=False)

    def _navigate_to(self, path: str, *, update_stack: bool = True) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self.status_var.set("低内存模式不加载目录明细")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            self.status_var.set("请等待当前扫描结束后再切换目录")
            return
        normalized = self._normalize_path(path)
        if not os.path.isdir(normalized):
            self.status_var.set("该目录已不存在，无法打开")
            return
        if update_stack:
            self.nav_stack = self._path_chain(normalized)
        self.path_var.set(normalized)
        self._refresh_breadcrumbs()
        skeleton_result = self._navigation_result_from_skeleton(normalized)
        if skeleton_result is not None:
            self._show_navigation_result(skeleton_result, "内存骨架")
            return
        cached = self.nav_cache.get(normalized)
        if cached is not None:
            self._show_navigation_result(cached, "本次缓存")
            return
        if not any(
            self._path_is_within(normalized, root)
            for root in self.nav_invalidated_roots
        ):
            snapshot_id = self.storage.latest_snapshot_id(normalized)
            if snapshot_id is not None:
                result = self.storage.load_snapshot(snapshot_id)
                if result is not None:
                    self.nav_cache[normalized] = result
                    self._show_navigation_result(result, "历史快照")
                    return
        self._start_scan(role="navigation", path=normalized)

    def _navigation_result_from_skeleton(
        self, path: str
    ) -> ScanResult | None:
        if self.navigation_skeleton is None:
            return None
        normalized = self._normalize_path(path)
        if any(
            self._path_is_within(normalized, invalidated)
            for invalidated in self.nav_invalidated_roots
        ):
            return None
        return materialize_navigation_result(
            self.navigation_skeleton, normalized
        )

    def _accept_navigation_skeleton(self, result: ScanResult) -> None:
        skeleton = result.skeleton
        if skeleton is None:
            return
        if self.navigation_skeleton is None:
            if self._normalize_path(result.root_path) == self._normalize_path(
                self.session_root_path
            ):
                self.navigation_skeleton = skeleton
        elif self._path_is_within(
            result.root_path, self.navigation_skeleton.root_path
        ):
            self.navigation_skeleton = merge_directory_skeleton(
                self.navigation_skeleton, skeleton
            )
        if self.navigation_skeleton is not None:
            self.nav_invalidated_roots = {
                invalidated
                for invalidated in self.nav_invalidated_roots
                if not self._path_is_within(invalidated, result.root_path)
            }

    def _show_navigation_result(self, result: ScanResult, source: str) -> None:
        self._show_scan_result(result, [], False, update_growth=False)
        data_time = result.finished_at.strftime("%Y-%m-%d %H:%M:%S")
        self.status_var.set(f"已从{source}载入 · 数据时间 {data_time}")
        self.detail_var.set(
            f"{result.root_path} · {self._result_space_text(result)} · "
            f"{result.file_count:,} 个文件 · 数据时间 {data_time}"
        )
        self._refresh_context_status()

    def _invalidate_navigation_cache(self, path: str) -> None:
        normalized = self._normalize_path(path)
        for cached_path in list(self.nav_cache):
            if self._path_is_within(cached_path, normalized):
                del self.nav_cache[cached_path]
        self.nav_invalidated_roots.add(normalized)

    def _refresh_breadcrumbs(self) -> None:
        for child in self.breadcrumb_frame.winfo_children():
            child.destroy()
        for index, path in enumerate(self.nav_stack):
            if index:
                ttk.Label(self.breadcrumb_frame, text="›").pack(
                    side=tk.LEFT, padx=2
                )
            label = path if index == 0 else os.path.basename(path)
            button = ttk.Button(
                self.breadcrumb_frame,
                text=label or path,
                command=lambda value=path: self._navigate_to(value),
                state=(
                    tk.DISABLED
                    if self.run_mode == RUN_MODE_LOW_MEMORY
                    else tk.NORMAL
                ),
            )
            button.pack(side=tk.LEFT)
        self.up_button.configure(
            state=(
                tk.NORMAL
                if self.run_mode == RUN_MODE_FULL and len(self.nav_stack) > 1
                else tk.DISABLED
            )
        )

    def _select_low_memory_reference(self) -> int | None:
        session_root = self._normalize_path(self.session_root_path)
        for snapshot_id in (
            self.automatic_current_snapshot_id,
            self.session_start_snapshot_id,
        ):
            if snapshot_id is None:
                continue
            info = self.storage.get_snapshot_info(snapshot_id)
            if (
                info is not None
                and self._normalize_path(info.root_path) == session_root
            ):
                return info.id
        return None

    def _request_run_mode(
        self,
        requested_mode: str,
        *,
        parent: tk.Misc | None = None,
    ) -> bool:
        if requested_mode not in RUN_MODE_LABELS:
            raise ValueError(f"未知运行模式：{requested_mode}")
        if requested_mode == self.run_mode:
            return True
        dialog_parent = parent or self.root
        if self.session_id is None or self.session_start_sample is None:
            messagebox.showinfo(
                "正在初始化",
                "请等待首次磁盘采样完成后再切换运行模式。",
                parent=dialog_parent,
            )
            return False
        if requested_mode == RUN_MODE_LOW_MEMORY:
            if self.active_scan_role is not None or (
                self.scan_thread and self.scan_thread.is_alive()
            ):
                messagebox.showinfo(
                    "扫描进行中",
                    "请等待当前扫描结束，或先取消扫描，再切换低内存模式。",
                    parent=dialog_parent,
                )
                return False
            self._enter_low_memory_mode()
            self._note_manual_mode_change("gui")
            return True
        switched = self._leave_low_memory_mode(parent=dialog_parent)
        if switched:
            self._note_manual_mode_change("gui")
        return switched

    def _toggle_run_mode(self) -> None:
        requested_mode = (
            RUN_MODE_LOW_MEMORY
            if self.run_mode == RUN_MODE_FULL
            else RUN_MODE_FULL
        )
        self._request_run_mode(requested_mode)

    def _enter_low_memory_mode(self) -> None:
        reference_id = self._select_low_memory_reference()
        try:
            sample = self._record_mode_boundary_sample()
        except Exception as error:
            self.logger.warning(
                "low_memory_boundary_sample_failed error=%s", error
            )
            sample = self.latest_disk_sample or self.session_start_sample
        if sample is None:
            raise RuntimeError("尚未取得磁盘采样，无法切换低内存模式")

        if self.baseline_after_id is not None:
            try:
                self.root.after_cancel(self.baseline_after_id)
            except tk.TclError:
                pass
            self.baseline_after_id = None
        self.baseline_pending = False
        self.low_memory_origin = "full"
        self.low_memory_started_at = sample.recorded_at
        self.low_memory_start_sample = sample
        self.low_memory_reference_snapshot_id = reference_id
        self.run_mode = RUN_MODE_LOW_MEMORY
        self.storage.set_setting("run_mode", self.run_mode)
        self._release_full_mode_state()
        self._apply_run_mode_ui()
        self._update_low_memory_change(sample)
        gc.collect()
        gc.collect()
        self.logger.info(
            "run_mode_changed mode=low_memory reference_snapshot_id=%s",
            reference_id,
        )
        self._update_tray_state()

    def _leave_low_memory_mode(
        self,
        *,
        should_scan: bool | None = None,
        parent: tk.Misc | None = None,
    ) -> bool:
        dialog_parent = parent or self.root
        cold_start = self.low_memory_origin == "cold"
        if cold_start:
            if should_scan is None:
                messagebox.showinfo(
                    "建立文件基线",
                    "此前只有磁盘口径记录，无文件地址明细；本次建立新基线。",
                    parent=dialog_parent,
                )
                should_scan = True
        elif should_scan is None:
            answer = messagebox.askyesnocancel(
                "切回全功能模式",
                "是否立即扫描当前监控路径并重建空间分布？\n\n"
                "是：立即补扫；否：只切换模式，稍后手动扫描；取消：保持低内存模式。",
                parent=dialog_parent,
            )
            if answer is None:
                return False
            should_scan = bool(answer)
        assert should_scan is not None

        reference_id = self.low_memory_reference_snapshot_id
        try:
            self._record_mode_boundary_sample()
        except Exception as error:
            self.logger.warning(
                "full_mode_boundary_sample_failed error=%s", error
            )
        self.run_mode = RUN_MODE_FULL
        self.storage.set_setting("run_mode", self.run_mode)
        self.treemap_placeholder_text = (
            "数据尚未重建，点击“重新扫描当前目录”生成空间分布图"
        )
        self._apply_run_mode_ui()
        self._show_no_realtime_snapshot(
            "全功能模式已恢复，尚无实时快照对比"
        )
        if should_scan:
            if cold_start:
                self.cold_low_memory_baseline_pending = True
                self._start_scan(role="baseline", path=self.session_root_path)
            else:
                self._start_scan(
                    role="low_memory_resume",
                    path=self.session_root_path,
                    reference_snapshot_id=reference_id,
                )
        else:
            self._draw_treemap(animate=False)
            self.status_var.set(
                "已切回全功能模式；目录数据尚未重建，可稍后手动扫描"
            )
        self.logger.info(
            "run_mode_changed mode=full rescan=%s reference_snapshot_id=%s",
            should_scan,
            reference_id,
        )
        self._update_tray_state()
        return True

    def _record_mode_boundary_sample(self) -> DiskSample:
        sample = read_disk_sample(self.session_root_path)
        self.storage.add_disk_sample(sample)
        self.current_drive = sample.drive
        self._reload_trend_data()
        self._update_metrics(sample)
        return sample

    def _release_full_mode_state(self) -> None:
        self._cancel_treemap_animation()
        if self.treemap_resize_after_id is not None:
            try:
                self.root.after_cancel(self.treemap_resize_after_id)
            except tk.TclError:
                pass
            self.treemap_resize_after_id = None
        self.current_result = None
        self.navigation_skeleton = None
        self.nav_cache.clear()
        self.nav_invalidated_roots.clear()
        self.rectangle_items.clear()
        self.rectangle_canvas_ids.clear()
        self.hovered_rectangle_path = None
        self.map_canvas.delete("all")
        self.change_context = None
        self.default_change_context = None
        self.default_growth_subtitle = ""
        self.default_baseline_info = ""
        self.growth_item_by_id.clear()
        for item_id in self.growth_tree.get_children():
            self.growth_tree.delete(item_id)
        self.automatic_baseline_snapshot_id = None
        self.automatic_current_snapshot_id = None
        self.latest_full_snapshot_id = None
        self.manual_baseline_mode = None
        self.last_scan_summary = "低内存模式未扫描"

    def _show_no_realtime_snapshot(self, subtitle: str) -> None:
        self.change_context = None
        self.snapshot_total_var.set("--")
        self.snapshot_total_label.configure(fg=COLORS["muted"])
        self.baseline_info_var.set("基线：未加载实时文件快照")
        self.comparison_info_var.set("当前比较：无实时文件快照")
        self.growth_subtitle_var.set(subtitle)
        self.growth_item_by_id.clear()
        for item_id in self.growth_tree.get_children():
            self.growth_tree.delete(item_id)
        self.growth_tree.insert("", tk.END, text=subtitle)

    def _apply_run_mode_ui(self) -> None:
        low_memory = self.run_mode == RUN_MODE_LOW_MEMORY
        self.mode_status_var.set(RUN_MODE_LABELS[self.run_mode])
        self.mode_button_var.set(
            "切换全功能模式" if low_memory else "切换低内存模式"
        )
        control_state = tk.DISABLED if low_memory or self.closing else tk.NORMAL
        self.mode_toggle_button.configure(
            state=tk.DISABLED if self.closing else tk.NORMAL
        )
        self.scan_button.configure(state=control_state)
        self.save_snapshot_button.configure(state=control_state)
        self.migration_advice_button.configure(
            state=(
                tk.NORMAL
                if self.current_result is not None and not self.closing
                else tk.DISABLED
            )
        )
        self.search_filter_button.configure(
            state=(
                tk.NORMAL
                if self.current_result is not None and not self.closing
                else tk.DISABLED
            )
        )
        self.choose_directory_button.configure(state=control_state)
        self.path_entry.configure(state=control_state)
        self.cancel_button.configure(state=tk.DISABLED)
        self.restore_baseline_button.configure(
            state=tk.DISABLED if low_memory else tk.NORMAL
        )
        if low_memory:
            self.treemap_placeholder_text = (
                "低内存模式：未扫描，切回全功能后可补扫"
            )
            self.detail_var.set(self.treemap_placeholder_text)
            self._show_no_realtime_snapshot(
                "低内存模式：无实时快照对比"
            )
            self.baseline_mode_var.set("低内存模式")
            self.baseline_mode_selector.configure(state=tk.DISABLED)
            self._draw_treemap(animate=False)
            self.status_var.set("已切换低内存模式：持续记录容量与趋势")
        self._refresh_breadcrumbs()
        self._refresh_context_status()

    def _open_settings(self, *, from_tray: bool = False) -> None:
        if self.settings_window is not None:
            try:
                if self.settings_window.winfo_exists():
                    self.settings_window.deiconify()
                    self.settings_window.state("normal")
                    self.settings_window.lift()
                    self.settings_window.focus_force()
                    return
            except tk.TclError:
                pass
            self.settings_window = None

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("C 盘空间增长监控器 · 设置")
        window.resizable(False, False)
        window.configure(bg=COLORS["background"])
        if self.app_icon_path.exists():
            window.iconbitmap(default=str(self.app_icon_path))
        if self._window_is_visible():
            window.transient(self.root)
        try:
            window.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass

        current_behavior = self.storage.get_setting("close_behavior", "ask")
        dialog = SettingsDialog(
            window,
            state=SettingsDialogState(
                close_behavior_label=CLOSE_BEHAVIOR_LABELS.get(
                    current_behavior, "每次询问"
                ),
                run_mode_label=RUN_MODE_LABELS[self.run_mode],
                autostart_enabled=is_autostart_enabled(),
                collect_file_space=self.collect_file_space,
                exclude_rules=self.exclude_rules,
                auto_mode_config=self.auto_mode_config,
            ),
            session_root_path=self.session_root_path,
            from_tray=from_tray,
            panel_background=COLORS["panel"],
            close_behavior_labels=CLOSE_BEHAVIOR_LABELS,
            run_mode_labels=RUN_MODE_LABELS,
            auto_rescan_labels=AUTO_RESCAN_LABELS,
            on_submit=lambda submission: self._save_settings_submission(
                submission, parent=window
            ),
            on_close=self._close_settings_window,
        )
        behavior_box = dialog.build()
        window.protocol("WM_DELETE_WINDOW", self._close_settings_window)
        window.bind("<Escape>", lambda _event: self._close_settings_window())
        window.bind("<Configure>", self._schedule_display_sync, add="+")
        window.grab_set()
        position_near_cursor(window)
        behavior_box.focus_set()
        window.lift()
        window.focus_force()

    def _save_settings_submission(
        self, submission: SettingsSubmission, *, parent: tk.Toplevel
    ) -> bool:
        if not self._request_run_mode(submission.run_mode, parent=parent):
            return False
        try:
            self.storage.set_setting("close_behavior", submission.close_behavior)
            self.collect_file_space = submission.collect_file_space
            self.storage.set_setting(
                "file_space_accounting",
                "exact" if self.collect_file_space else "logical",
            )
            self.exclude_rules = submission.exclude_rules
            self.storage.set_setting("exclude_rules", "\n".join(self.exclude_rules))
            set_autostart(submission.autostart_enabled)
            self._apply_automation_config(submission.auto_mode_config)
        except (OSError, ValueError) as error:
            messagebox.showerror("设置失败", str(error), parent=parent)
            return False
        self.status_var.set("设置已保存")
        return True

    def _close_settings_window(self) -> None:
        window = self.settings_window
        self.settings_window = None
        if window is None:
            return
        try:
            if window.grab_current() is window:
                window.grab_release()
        except tk.TclError:
            pass
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            pass

    def _open_migration_advice(self) -> None:
        if self.current_result is None:
            messagebox.showinfo("迁移建议", "当前没有可用的扫描结果", parent=self.root)
            return
        if self.migration_window is not None:
            try:
                if self.migration_window.winfo_exists():
                    self.migration_window.deiconify()
                    self.migration_window.lift()
                    self.migration_window.focus_force()
                    return
            except tk.TclError:
                pass
            self.migration_window = None

        window = tk.Toplevel(self.root)
        self.migration_window = window
        window.title("C 盘空间增长监控器 · 迁移建议")
        self._configure_dialog_window(
            window,
            width=980,
            height=680,
            minimum_width=820,
            minimum_height=560,
        )
        window.configure(bg=COLORS["background"])
        window.columnconfigure(0, weight=1)
        window.rowconfigure(3, weight=1)
        if self.app_icon_path.exists():
            window.iconbitmap(default=str(self.app_icon_path))
        if self._window_is_visible():
            window.transient(self.root)

        header = ttk.Frame(window, padding=(18, 16, 18, 10))
        header.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(header, text="迁移建议视图", style="DialogTitle.TLabel").pack(
            anchor=tk.W
        )
        ttk.Label(
            header,
            text=SAFETY_NOTICE,
            style="Subtitle.TLabel",
            wraplength=900,
        ).pack(anchor=tk.W, pady=(4, 0))

        filters = ttk.Frame(window, style="Panel.TFrame", padding=12)
        filters.grid(row=1, column=0, sticky=tk.EW, padx=18)
        self.migration_target_var = tk.StringVar()
        self.migration_extension_var = tk.StringVar()
        self.migration_min_mb_var = tk.StringVar(value="0")
        ttk.Label(filters, text="目标盘", background=COLORS["panel"]).grid(
            row=0, column=0, sticky=tk.W
        )
        target_entry = ttk.Entry(
            filters, textvariable=self.migration_target_var, state="readonly"
        )
        target_entry.grid(row=0, column=1, sticky=tk.EW, padx=(8, 8))

        def choose_target() -> None:
            selected = filedialog.askdirectory(
                parent=window,
                title="选择用于空间估算的目标盘或目录",
                mustexist=True,
            )
            if selected:
                self.migration_target_var.set(selected)

        ttk.Button(filters, text="选择目标盘", command=choose_target).grid(
            row=0, column=2, padx=(0, 14)
        )
        ttk.Label(filters, text="扩展名", background=COLORS["panel"]).grid(
            row=0, column=3, sticky=tk.W
        )
        ttk.Entry(
            filters, textvariable=self.migration_extension_var, width=9
        ).grid(row=0, column=4, padx=(6, 14))
        ttk.Label(filters, text="最小 MB", background=COLORS["panel"]).grid(
            row=0, column=5, sticky=tk.W
        )
        ttk.Entry(filters, textvariable=self.migration_min_mb_var, width=8).grid(
            row=0, column=6, padx=(6, 10)
        )
        filters.columnconfigure(1, weight=1)

        self.migration_status_var = tk.StringVar(
            value="请选择目标盘，然后刷新建议。空间为读取时的即时值。"
        )
        ttk.Label(
            window,
            textvariable=self.migration_status_var,
            style="Subtitle.TLabel",
            padding=(18, 10, 18, 8),
        ).grid(row=2, column=0, sticky=tk.EW)

        results = ttk.Panedwindow(window, orient=tk.VERTICAL)
        results.grid(
            row=3,
            column=0,
            sticky=tk.NSEW,
            padx=18,
            pady=(0, 10),
        )
        candidate_panel = ttk.Labelframe(results, text="可考虑迁移")
        excluded_panel = ttk.Labelframe(results, text="保守排除说明")
        results.add(candidate_panel, weight=1)
        results.add(excluded_panel, weight=1)
        self.migration_candidate_tree = ttk.Treeview(
            candidate_panel,
            columns=("logical", "estimate", "basis"),
            show="tree headings",
            height=8,
        )
        self.migration_candidate_tree.heading("#0", text="完整路径")
        self.migration_candidate_tree.heading("logical", text="逻辑大小")
        self.migration_candidate_tree.heading("estimate", text="估算占用")
        self.migration_candidate_tree.heading("basis", text="估算依据")
        self.migration_candidate_tree.column("#0", width=560, stretch=True)
        self.migration_candidate_tree.column("logical", width=110, anchor=tk.E)
        self.migration_candidate_tree.column("estimate", width=110, anchor=tk.E)
        self.migration_candidate_tree.column("basis", width=130)
        self.migration_candidate_tree.pack(fill=tk.BOTH, expand=True)
        self.migration_excluded_tree = ttk.Treeview(
            excluded_panel,
            columns=("logical", "reason"),
            show="tree headings",
            height=8,
        )
        self.migration_excluded_tree.heading("#0", text="完整路径")
        self.migration_excluded_tree.heading("logical", text="逻辑大小")
        self.migration_excluded_tree.heading("reason", text="排除原因")
        self.migration_excluded_tree.column("#0", width=520, stretch=True)
        self.migration_excluded_tree.column("logical", width=110, anchor=tk.E)
        self.migration_excluded_tree.column("reason", width=300)
        self.migration_excluded_tree.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(window, padding=(18, 0, 18, 16))
        footer.grid(row=4, column=0, sticky=tk.EW)

        def refresh() -> None:
            target = self.migration_target_var.get().strip()
            if not target:
                messagebox.showerror(
                    "目标盘未选择", "请先明确选择目标盘或目录", parent=window
                )
                return
            extension = self.migration_extension_var.get().strip() or None
            try:
                minimum_mb = float(self.migration_min_mb_var.get().strip() or "0")
                if minimum_mb < 0:
                    raise ValueError("最小 MB 不能为负数")
            except ValueError as error:
                messagebox.showerror("筛选无效", str(error), parent=window)
                return
            result = self.current_result
            if result is None:
                messagebox.showerror(
                    "扫描结果不可用", "当前扫描结果已释放，请重新扫描", parent=window
                )
                return
            self.migration_refresh_button.configure(state=tk.DISABLED)
            self.migration_status_var.set("正在读取已记录文件状态和目标盘空间……")

            def worker() -> None:
                try:
                    advice = build_migration_advice(
                        tuple(result.items),
                        target,
                        active_data_directory=self.storage.database_path.parent,
                        extension=extension,
                        min_size=int(minimum_mb * 1024 * 1024),
                        limit=200,
                        inspection_limit=1_000,
                    )
                except Exception as error:
                    self.messages.put(("migration_advice_error", str(error)))
                else:
                    self.messages.put(("migration_advice_done", advice))

            threading.Thread(target=worker, daemon=True).start()

        self.migration_refresh_button = ttk.Button(
            footer, text="刷新建议", command=refresh, style="Accent.TButton"
        )
        self.migration_refresh_button.pack(side=tk.RIGHT)
        ttk.Button(
            footer, text="关闭", command=self._close_migration_advice
        ).pack(side=tk.RIGHT, padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", self._close_migration_advice)
        window.bind("<Escape>", lambda _event: self._close_migration_advice())
        window.bind("<Configure>", self._schedule_display_sync, add="+")
        position_near_cursor(window)
        window.lift()
        window.focus_force()

    def _render_migration_advice(self, advice: dict) -> None:
        window = self.migration_window
        if window is None or not window.winfo_exists():
            return
        for tree in (self.migration_candidate_tree, self.migration_excluded_tree):
            for item_id in tree.get_children():
                tree.delete(item_id)
        for item in advice["candidates"]:
            basis = (
                "唯一分配大小"
                if item["estimate_basis"] == "unique_allocated_size"
                else "逻辑大小（保守）"
            )
            self.migration_candidate_tree.insert(
                "",
                tk.END,
                text=item["path"],
                values=(
                    format_bytes(item["logical_size_bytes"]),
                    format_bytes(item["estimated_size_bytes"]),
                    basis,
                ),
            )
        for item in advice["excluded"]:
            reasons = "；".join(
                MIGRATION_REASON_LABELS.get(code, code)
                for code in item["reason_codes"]
            )
            self.migration_excluded_tree.insert(
                "",
                tk.END,
                text=item["path"],
                values=(format_bytes(item["logical_size_bytes"]), reasons),
            )
        target = advice["target"]
        remaining = format_bytes(abs(target["estimated_remaining_bytes"]))
        if target["space_sufficient"]:
            space_text = f"保守剩余 {remaining}"
        else:
            space_text = f"空间不足，缺少 {remaining}"
        self.migration_status_var.set(
            f"目标盘可用 {format_bytes(target['free_bytes'])} · "
            f"建议合计 {format_bytes(advice['estimated_total_bytes'])} · "
            f"{space_text} · 候选 {len(advice['candidates'])} / "
            f"排除 {advice['excluded_count']}"
        )
        self.migration_refresh_button.configure(state=tk.NORMAL)

    def _close_migration_advice(self) -> None:
        window = self.migration_window
        self.migration_window = None
        if window is None:
            return
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            pass

    def _open_current_snapshot_browser(self) -> None:
        if self.current_result is None or self.current_result.snapshot_id is None:
            messagebox.showinfo(
                "查找/筛选", "当前结果尚未保存为可查询快照", parent=self.root
            )
            return
        self._open_snapshot_browser(self.current_result.snapshot_id)

    def _open_selected_snapshot_browser(self) -> None:
        selected = self.history_tree.selection()
        if len(selected) != 1:
            return
        snapshot = self.history_items_by_id.get(selected[0])
        if snapshot is not None:
            self._open_snapshot_browser(snapshot.id)

    def _open_snapshot_browser(self, snapshot_id: int) -> None:
        self._close_snapshot_browser()
        database = ReadOnlyDatabase(self.storage.database_path)
        try:
            snapshot = database.snapshot_info(snapshot_id)
        except Exception as error:
            messagebox.showerror("快照不可用", str(error), parent=self.root)
            return
        if snapshot is None:
            messagebox.showerror("快照不可用", "指定快照不存在", parent=self.root)
            return

        window = tk.Toplevel(self.root)
        self.snapshot_browser_window = window
        window.title(f"快照 #{snapshot_id} · 深层查看与查找")
        self._configure_dialog_window(
            window,
            width=1050,
            height=680,
            minimum_width=860,
            minimum_height=560,
        )
        window.configure(bg=COLORS["background"])
        if self.app_icon_path.exists():
            window.iconbitmap(default=str(self.app_icon_path))
        if self._window_is_visible():
            window.transient(self.root)

        header = ttk.Frame(window, padding=(18, 14, 18, 8))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text=f"快照 #{snapshot_id} · {snapshot['finished_at']}",
            style="DialogTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text=(
                f"根路径：{snapshot['root_path']} · "
                f"深层状态：{snapshot['directory_summary_state']} · "
                f"排除 {snapshot['excluded_rule_count']} 条规则 / "
                f"{snapshot['excluded_item_count']} 个对象"
            ),
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(3, 0))

        controls = ttk.Frame(window, style="Panel.TFrame", padding=10)
        controls.pack(fill=tk.X, padx=18)
        path_var = tk.StringVar(value=snapshot["root_path"])
        query_var = tk.StringVar()
        mode_var = tk.StringVar(value="子串")
        kind_var = tk.StringVar(value="全部")
        extension_var = tk.StringVar()
        min_mb_var = tk.StringVar()
        max_mb_var = tk.StringVar()
        modified_after_var = tk.StringVar()
        modified_before_var = tk.StringVar()
        ttk.Label(controls, text="当前目录", background=COLORS["panel"]).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Entry(controls, textvariable=path_var, state="readonly").grid(
            row=0, column=1, columnspan=5, sticky=tk.EW, padx=(6, 8)
        )
        ttk.Label(controls, text="搜索", background=COLORS["panel"]).grid(
            row=1, column=0, sticky=tk.W, pady=(8, 0)
        )
        query_entry = ttk.Entry(controls, textvariable=query_var, width=24)
        query_entry.grid(
            row=1, column=1, sticky=tk.EW, padx=(6, 8), pady=(8, 0)
        )
        ttk.Combobox(
            controls,
            textvariable=mode_var,
            values=("路径前缀", "子串"),
            state="readonly",
            width=9,
        ).grid(row=1, column=2, padx=(0, 8), pady=(8, 0))
        ttk.Combobox(
            controls,
            textvariable=kind_var,
            values=("全部", "文件", "目录"),
            state="readonly",
            width=7,
        ).grid(row=1, column=3, padx=(0, 8), pady=(8, 0))
        ttk.Label(controls, text="扩展名", background=COLORS["panel"]).grid(
            row=1, column=4, sticky=tk.E, pady=(8, 0)
        )
        ttk.Entry(controls, textvariable=extension_var, width=9).grid(
            row=1, column=5, sticky=tk.W, padx=(6, 0), pady=(8, 0)
        )
        ttk.Label(controls, text="大小 MB", background=COLORS["panel"]).grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0)
        )
        size_frame = ttk.Frame(controls, style="Panel.TFrame")
        size_frame.grid(row=2, column=1, sticky=tk.W, padx=(6, 8), pady=(8, 0))
        ttk.Entry(size_frame, textvariable=min_mb_var, width=8).pack(side=tk.LEFT)
        ttk.Label(size_frame, text=" 至 ", background=COLORS["panel"]).pack(
            side=tk.LEFT
        )
        ttk.Entry(size_frame, textvariable=max_mb_var, width=8).pack(side=tk.LEFT)
        ttk.Label(controls, text="修改日期", background=COLORS["panel"]).grid(
            row=2, column=2, sticky=tk.E, pady=(8, 0)
        )
        date_frame = ttk.Frame(controls, style="Panel.TFrame")
        date_frame.grid(
            row=2, column=3, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(8, 0)
        )
        ttk.Entry(date_frame, textvariable=modified_after_var, width=12).pack(
            side=tk.LEFT
        )
        ttk.Label(date_frame, text=" 至 ", background=COLORS["panel"]).pack(
            side=tk.LEFT
        )
        ttk.Entry(date_frame, textvariable=modified_before_var, width=12).pack(
            side=tk.LEFT
        )
        ttk.Label(
            date_frame, text=" YYYY-MM-DD", background=COLORS["panel"]
        ).pack(side=tk.LEFT)
        controls.columnconfigure(1, weight=1)

        button_row = ttk.Frame(window, padding=(18, 8, 18, 6))
        button_row.pack(fill=tk.X)
        status_var = tk.StringVar(value="正在读取快照目录……")
        ttk.Label(button_row, textvariable=status_var, style="Subtitle.TLabel").pack(
            anchor=tk.W, fill=tk.X
        )
        action_row = ttk.Frame(button_row)
        action_row.pack(fill=tk.X, pady=(6, 0))
        up_button = ttk.Button(action_row, text="上一级")
        browse_button = ttk.Button(action_row, text="浏览当前层")
        search_button = ttk.Button(action_row, text="搜索")
        largest_button = ttk.Button(action_row, text="最大文件")
        next_button = ttk.Button(action_row, text="下一页", state=tk.DISABLED)
        for button in (
            up_button,
            browse_button,
            search_button,
            largest_button,
            next_button,
        ):
            button.pack(side=tk.LEFT, padx=(0, 6))

        tree = ttk.Treeview(
            window,
            columns=("kind", "size", "modified", "state"),
            show="tree headings",
        )
        tree.heading("#0", text="完整路径 / 聚合项")
        tree.heading("kind", text="类型")
        tree.heading("size", text="逻辑大小")
        tree.heading("modified", text="修改时间")
        tree.heading("state", text="统计状态")
        tree.column("#0", width=600, stretch=True)
        tree.column("kind", width=80, anchor=tk.CENTER)
        tree.column("size", width=110, anchor=tk.E)
        tree.column("modified", width=145)
        tree.column("state", width=110)
        scrollbar = ttk.Scrollbar(window, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 18), pady=(0, 12))
        tree.pack(fill=tk.BOTH, expand=True, padx=(18, 0), pady=(0, 12))
        item_by_id: dict[str, dict] = {}
        next_cursor: int | None = None
        last_action: str | None = None

        def render(data: dict) -> None:
            item_by_id.clear()
            for item_id in tree.get_children():
                tree.delete(item_id)
            for index, item in enumerate(data["items"]):
                item_id = f"result-{index}"
                item_by_id[item_id] = item
                modified = item.get("modified_at")
                modified_text = (
                    datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(modified, (int, float)) and modified > 0
                    else "--"
                )
                kind_text = {
                    "file": "文件",
                    "directory": "目录",
                    "aggregate": "未记录明细",
                }.get(item.get("kind"), str(item.get("kind", "")))
                display_path = item.get("path") or item.get("name", "未记录文件明细")
                tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    text=display_path,
                    values=(
                        kind_text,
                        format_bytes(int(item.get("size_bytes", 0))),
                        modified_text,
                        item.get("measurement_state", "--"),
                    ),
                )
            status_var.set(
                f"{data.get('coverage', '')} · 本页 {len(data['items'])} 项"
            )

        def parse_filters() -> dict:
            def parse_mb(value: str) -> int | None:
                stripped = value.strip()
                if not stripped:
                    return None
                parsed = float(stripped)
                if parsed < 0:
                    raise ValueError("大小不能为负数")
                return int(parsed * 1024 * 1024)

            def parse_modified(value: str, *, end_of_day: bool) -> float | None:
                stripped = value.strip()
                if not stripped:
                    return None
                parsed = datetime.fromisoformat(stripped)
                if len(stripped) == 10 and end_of_day:
                    parsed = parsed.replace(hour=23, minute=59, second=59)
                return parsed.timestamp()

            return {
                "extension": extension_var.get().strip() or None,
                "min_size": parse_mb(min_mb_var.get()),
                "max_size": parse_mb(max_mb_var.get()),
                "modified_after": parse_modified(
                    modified_after_var.get(), end_of_day=False
                ),
                "modified_before": parse_modified(
                    modified_before_var.get(), end_of_day=True
                ),
            }

        def browse(path: str | None = None) -> None:
            nonlocal next_cursor, last_action
            try:
                data = database.snapshot_tree(
                    snapshot_id, path or path_var.get(), limit=200
                )
            except Exception as error:
                messagebox.showerror("目录不可用", str(error), parent=window)
                return
            path_var.set(data["path"])
            next_cursor = None
            last_action = None
            next_button.configure(state=tk.DISABLED)
            root_path = os.path.normcase(os.path.abspath(snapshot["root_path"]))
            up_button.configure(
                state=(
                    tk.DISABLED
                    if os.path.normcase(os.path.abspath(data["path"])) == root_path
                    else tk.NORMAL
                )
            )
            render(data)

        def run_filtered(action: str, cursor: int = 0) -> None:
            nonlocal next_cursor, last_action
            try:
                filters = parse_filters()
                if action == "search":
                    data = database.search_snapshot(
                        snapshot_id,
                        query_var.get(),
                        mode=(
                            "prefix" if mode_var.get() == "路径前缀" else "substring"
                        ),
                        kind={"全部": "any", "文件": "file", "目录": "directory"}[
                            kind_var.get()
                        ],
                        limit=200,
                        cursor=cursor,
                        **filters,
                    )
                else:
                    data = database.largest_snapshot_files(
                        snapshot_id,
                        limit=200,
                        cursor=cursor,
                        **filters,
                    )
            except Exception as error:
                messagebox.showerror("查询无效", str(error), parent=window)
                return
            next_cursor = data["next_cursor"]
            last_action = action
            next_button.configure(
                state=tk.NORMAL if next_cursor is not None else tk.DISABLED
            )
            render(data)

        def go_up() -> None:
            current = os.path.normcase(os.path.abspath(path_var.get()))
            root_path = os.path.normcase(os.path.abspath(snapshot["root_path"]))
            parent = os.path.dirname(current)
            if self._path_is_within(parent, root_path):
                browse(parent)

        def open_selected(_event: tk.Event | None = None) -> None:
            selected = tree.selection()
            if not selected:
                return
            item = item_by_id.get(selected[0])
            if item and item.get("kind") == "directory" and item.get("path"):
                browse(item["path"])

        def load_next() -> None:
            if last_action is not None and next_cursor is not None:
                run_filtered(last_action, next_cursor)

        up_button.configure(command=go_up)
        browse_button.configure(command=browse)
        search_button.configure(command=lambda: run_filtered("search"))
        largest_button.configure(command=lambda: run_filtered("largest"))
        next_button.configure(command=load_next)
        query_entry.bind("<Return>", lambda _event: run_filtered("search"))
        tree.bind("<Double-Button-1>", open_selected)
        window.protocol("WM_DELETE_WINDOW", self._close_snapshot_browser)
        window.bind("<Escape>", lambda _event: self._close_snapshot_browser())
        window.bind("<Configure>", self._schedule_display_sync, add="+")
        position_near_cursor(window)
        browse(snapshot["root_path"])
        window.lift()
        window.focus_force()

    def _close_snapshot_browser(self) -> None:
        window = self.snapshot_browser_window
        self.snapshot_browser_window = None
        if window is None:
            return
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            pass

    def _sample_now(self) -> None:
        self.sample_after_id = None
        if self.closing:
            return
        try:
            monitor_path = (
                self.session_root_path
                if self.session_id is not None
                else (self.path_var.get() or "C:\\")
            )
            first_sample = self.session_id is None
            previous_sample = None
            if first_sample:
                previous_sample = self.storage.get_latest_disk_sample(
                    normalize_drive(monitor_path)
                )
            sample = read_disk_sample(monitor_path)
            if first_sample:
                self.blind_spot_result = calculate_blind_spot(
                    sample, previous_sample
                )
                self._show_blind_spot()
                self.storage.prune_disk_samples(retention_days=30)
                self.storage.prune_snapshots(retention_days=90)
                self.storage.recover_active_sessions(sample)
                self.startup_previous_session = (
                    self.storage.latest_completed_session()
                )
                self.session_root_path = os.path.abspath(self.path_var.get() or "C:\\")
                self.session_start_sample = sample
                self.session_id = self.storage.start_session(
                    sample, self.session_root_path
                )
                if self.run_mode == RUN_MODE_LOW_MEMORY:
                    self.low_memory_origin = "cold"
                    self.low_memory_started_at = sample.recorded_at
                    self.low_memory_start_sample = sample
                    self.low_memory_reference_snapshot_id = None
                    self.baseline_pending = False
                    self.status_var.set(
                        "低内存模式已启动：持续记录磁盘趋势，不建立目录基线"
                    )
                    self.logger.info(
                        "low_memory_session_started sample_at=%s",
                        sample.recorded_at.isoformat(timespec="seconds"),
                    )
                else:
                    self.baseline_pending = True
                    self.baseline_after_id = self.root.after(
                        300,
                        lambda: self._start_scan(
                            role="baseline", path=self.session_root_path
                        ),
                    )
                if self.startup_path_was_invalid:
                    self.status_var.set("启动路径无效，已自动回退到 C:\\")
            self.storage.add_disk_sample(sample)
            self.current_drive = sample.drive
            self._reload_trend_data()
            self._update_metrics(sample)
        except Exception as error:
            self._record_ui_error("读取磁盘信息", error)
        finally:
            if not self.closing:
                self.sample_after_id = self.root.after(
                    self.SAMPLE_INTERVAL_MS, self._sample_now
                )

    def _update_metrics(self, sample: DiskSample) -> None:
        self.latest_disk_sample = sample
        self.total_var.set(format_bytes(sample.total_bytes))
        self.used_var.set(format_bytes(sample.used_bytes))
        free_percent = (
            sample.free_bytes / sample.total_bytes * 100
            if sample.total_bytes
            else 0
        )
        self.free_var.set(
            f"{format_bytes(sample.free_bytes)} · {free_percent:.0f}%"
        )
        if self.session_start_sample is not None:
            change = sample.used_bytes - self.session_start_sample.used_bytes
            prefix = "+" if change > 0 else ""
            self.change_var.set(f"{prefix}{format_bytes(change)}")
            if change > 0:
                self.change_label.configure(style="ChangeIncrease.TLabel")
            elif change < 0:
                self.change_label.configure(style="ChangeDecrease.TLabel")
            else:
                self.change_label.configure(style="MetricValue.TLabel")
        else:
            self.change_var.set("建立基线")
        self._update_runtime_change(sample)
        self._update_low_memory_change(sample)

    def _update_low_memory_change(self, sample: DiskSample) -> None:
        start_sample = self.low_memory_start_sample
        if start_sample is None:
            self.low_memory_change_var.set("尚未进入低内存模式")
            self.low_memory_change_label.configure(fg=COLORS["muted"])
            return
        change = sample.used_bytes - start_sample.used_bytes
        prefix = "+" if change > 0 else ""
        self.low_memory_change_var.set(
            f"{prefix}{format_bytes(change)} · "
            f"{start_sample.recorded_at:%H:%M:%S} → {sample.recorded_at:%H:%M:%S} "
            "· 仅磁盘口径，无文件地址明细"
        )
        color = (
            COLORS["warning"]
            if change > 0
            else "#15803d" if change < 0 else COLORS["muted"]
        )
        self.low_memory_change_label.configure(fg=color)

    def _update_runtime_change(self, sample: DiskSample) -> None:
        if self.session_start_sample is None:
            self.runtime_change_var.set("等待本次会话启动采样")
            self.runtime_change_label.configure(fg=COLORS["muted"])
            return
        change = sample.used_bytes - self.session_start_sample.used_bytes
        prefix = "+" if change > 0 else ""
        time_range = (
            f"{self.session_start_sample.recorded_at:%H:%M:%S} → "
            f"{sample.recorded_at:%H:%M:%S}"
        )
        message = f"{prefix}{format_bytes(change)} · {time_range}"
        if change:
            selected_baseline_id = (
                self.change_context[2]
                if self.change_context is not None
                and self.change_context[0] == "snapshots"
                else None
            )
            has_runtime_snapshot = (
                self.automatic_current_snapshot_id is not None
                and self.automatic_current_snapshot_id
                != self.session_start_snapshot_id
            )
            if (
                selected_baseline_id == self.session_start_snapshot_id
                and has_runtime_snapshot
            ):
                current_info = self.storage.get_snapshot_info(
                    self.automatic_current_snapshot_id
                )
                if current_info is not None:
                    message += (
                        f" · 文件定位数据截至 {current_info.finished_at:%H:%M:%S}"
                    )
            else:
                message += " · 切换到“本次启动快照”并重新扫描可定位"
            session_root = self._normalize_path(self.session_root_path)
            drive_root = self._normalize_path(normalize_drive(self.session_root_path))
            if session_root != drive_root:
                message += "；当前只定位监控目录"
        self.runtime_change_var.set(message)
        color = (
            COLORS["warning"]
            if change > 0
            else "#15803d" if change < 0 else COLORS["muted"]
        )
        self.runtime_change_label.configure(fg=color)

    def _change_trend_range(self, _event: tk.Event | None = None) -> None:
        self.trend_hours = {
            "24 小时": 24,
            "7 天": 24 * 7,
            "30 天": 24 * 30,
        }.get(self.trend_range_var.get(), 24)
        self.trend_title_var.set(
            {
                24: "最近 24 小时已用空间",
                24 * 7: "最近 7 天已用空间",
                24 * 30: "最近 30 天已用空间",
            }[self.trend_hours]
        )
        self._reload_trend_data()

    def _reload_trend_data(self) -> None:
        if self.current_drive is None:
            self._draw_trend()
            return
        self.latest_samples = self.storage.get_disk_samples(
            self.current_drive, hours=self.trend_hours
        )
        self.session_boundaries = self.storage.get_session_boundaries(
            self.current_drive, hours=self.trend_hours
        )
        self._draw_trend()

    def _start_scan(
        self,
        role: str = "manual",
        path: str | None = None,
        *,
        reference_snapshot_id: int | None = None,
    ) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self.status_var.set("低内存模式不执行目录扫描")
            return
        if self.closing and role != "closing":
            return
        if self.scan_thread and self.scan_thread.is_alive():
            if role == "baseline":
                self.baseline_pending = True
            return
        scan_path_value = os.path.abspath(path or self.path_var.get().strip())
        if not os.path.isdir(scan_path_value) and role == "baseline":
            fallback_path = os.path.abspath("C:\\")
            if os.path.isdir(fallback_path):
                scan_path_value = fallback_path
                self.session_root_path = fallback_path
                self.path_var.set(fallback_path)
                self.status_var.set("基线路径无效，已自动回退到 C:\\")
        if not os.path.isdir(scan_path_value):
            messagebox.showerror("无法扫描", "请输入一个存在的目录路径。")
            if role == "baseline":
                self.baseline_pending = False
                self.status_var.set("基线未建立，关闭时将只保存总增长")
            return

        if role != "closing":
            self.path_var.set(scan_path_value)
        if role == "baseline":
            self.baseline_pending = False
        self.active_scan_role = role
        self.cancel_event = threading.Event()
        self.scan_button.configure(state=tk.DISABLED)
        self.save_snapshot_button.configure(state=tk.DISABLED)
        self.migration_advice_button.configure(state=tk.DISABLED)
        self.search_filter_button.configure(state=tk.DISABLED)
        self.up_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(
            state=tk.DISABLED if role == "closing" else tk.NORMAL
        )
        self.path_entry.configure(state=tk.DISABLED)
        self.progress.start(12)
        status_text = {
            "baseline": "正在建立本次启动的目录基线……",
            "closing": "正在保存关闭快照，完成后程序会自动退出……",
            "navigation": "没有可用缓存，正在扫描该目录……",
            "manual_save": "正在建立带备注的手动快照……",
            "low_memory_resume": "正在补扫当前路径并重建全功能数据……",
        }.get(role, "正在准备扫描……")
        self.status_var.set(status_text)
        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(scan_path_value, role, reference_snapshot_id),
            daemon=True,
        )
        self.scan_thread.start()
        self._update_tray_state()

    def _scan_worker(
        self,
        path: str,
        role: str,
        reference_snapshot_id: int | None = None,
    ) -> None:
        try:
            result = scan_path(
                path,
                cancel_event=self.cancel_event,
                progress_callback=lambda value: self.messages.put(
                    ("scan_progress", value)
                ),
                collect_file_space=self.collect_file_space,
                exclude_rules=self.exclude_rules,
            )
            note = self.pending_scan_note if role == "manual_save" else None
            snapshot_source = "manual" if role == "low_memory_resume" else role
            self.storage.save_scan(result, note=note, source=snapshot_source)
            self.storage.prune_directory_metrics()
            if role == "baseline":
                if self.session_id is not None and result.snapshot_id is not None:
                    self.storage.set_session_start_snapshot(
                        self.session_id, result.snapshot_id
                    )
                    self.session_start_snapshot_id = result.snapshot_id
                self.messages.put(("scan_done", role, result, [], False, None))
                return
            if role == "closing":
                final_sample = read_disk_sample(self.session_root_path)
                self.storage.add_disk_sample(final_sample)
                growth: list[GrowthItem] = []
                if self.session_id is not None:
                    growth = self.storage.finish_session(
                        self.session_id,
                        final_sample,
                        end_snapshot_id=result.snapshot_id,
                        end_reason="normal_close",
                    )
                self.messages.put(("close_done", result, growth, final_sample))
                return
            if role == "navigation":
                self.messages.put(("scan_done", role, result, [], False, None))
                return
            if (
                self.session_start_snapshot_id is None
                and result.snapshot_id is not None
                and result.root_path
                == os.path.normcase(os.path.abspath(self.session_root_path))
                and self.session_id is not None
            ):
                self.storage.set_session_start_snapshot(
                    self.session_id, result.snapshot_id
                )
                self.session_start_snapshot_id = result.snapshot_id
            if role == "low_memory_resume":
                growth: list[GrowthItem] = []
                if (
                    reference_snapshot_id is not None
                    and result.snapshot_id is not None
                ):
                    growth = self.storage.compare_snapshots(
                        result.snapshot_id,
                        reference_snapshot_id,
                        limit=100,
                    )
                self.messages.put(
                    (
                        "scan_done",
                        role,
                        result,
                        growth,
                        reference_snapshot_id is not None,
                        reference_snapshot_id,
                    )
                )
                return
            previous_id = self.storage.previous_snapshot_id(result)
            growth: list[GrowthItem] = []
            if previous_id is not None and result.snapshot_id is not None:
                growth = self.storage.compare_snapshots(
                    result.snapshot_id, previous_id, limit=100
                )
            self.messages.put(
                (
                    "scan_done",
                    role,
                    result,
                    growth,
                    previous_id is not None,
                    previous_id,
                )
            )
        except ScanCancelled:
            self.messages.put(("scan_cancelled", role))
        except Exception as error:
            self.logger.exception(
                "scan_worker_failed role=%s path=%s", role, path
            )
            self.messages.put(("scan_error", role, str(error)))

    def _cancel_scan(self) -> None:
        if self.active_scan_role == "baseline":
            self.baseline_pending = False
        self.cancel_event.set()
        self.status_var.set("正在安全停止扫描……")

    def _poll_messages(self) -> None:
        self.poll_after_id = None
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "tray_command":
                    self._handle_tray_command(message[1])
                elif kind == "scan_progress":
                    self.agent_controller.on_scan_progress(message[1])
                    self._show_progress(message[1])
                elif kind == "scan_done":
                    role, result, growth, has_baseline, previous_id = message[1:]
                    try:
                        self._accept_navigation_skeleton(result)
                        self.nav_cache[self._normalize_path(result.root_path)] = result
                        self.history_loaded = False
                        if role == "baseline":
                            self.session_start_snapshot_id = result.snapshot_id
                            if self.cold_low_memory_baseline_pending:
                                self.cold_low_memory_baseline_pending = False
                                self._apply_cold_low_memory_baseline(result)
                            else:
                                self._apply_startup_baseline(result)
                                self.status_var.set(
                                    "启动目录基线已建立，正在记录本次变化"
                                )
                                if self.test_low_after_baseline:
                                    self.test_low_after_baseline = False
                                    self.root.after_idle(
                                        lambda: self._request_run_mode(
                                            RUN_MODE_LOW_MEMORY
                                        )
                                    )
                        elif role == "navigation":
                            self._show_navigation_result(result, "新扫描")
                        elif role == "low_memory_resume":
                            self._apply_low_memory_resume(
                                result, growth, previous_id
                            )
                        else:
                            self._update_change_context_after_scan(
                                result, previous_id
                            )
                            self._show_scan_result(result, growth, has_baseline)
                            if role == "manual_save":
                                self.status_var.set(
                                    f"手动快照“{self.pending_scan_note}”已保存"
                                )
                        if self.notebook.select() == str(self.history_tab):
                            self._load_snapshot_history(reset=True)
                        self.agent_controller.on_scan_completed(role, result)
                    except Exception as error:
                        self.agent_controller.on_scan_failed(role, str(error))
                        self._record_ui_error("扫描结果界面处理", error)
                    finally:
                        self._finish_scan_ui()
                        self.logger.info("scan_ui_finished role=%s", role)
                        if self.closing:
                            self._begin_close_snapshot()
                        else:
                            self._start_pending_baseline()
                elif kind == "scan_cancelled":
                    self.agent_controller.on_scan_cancelled(message[1])
                    self.status_var.set("扫描已取消，未保存不完整快照")
                    self._finish_scan_ui()
                    if self.closing:
                        self._begin_close_snapshot()
                    else:
                        self._start_pending_baseline()
                elif kind == "scan_error":
                    role, error = message[1:]
                    self.agent_controller.on_scan_failed(role, error)
                    self.status_var.set(f"扫描失败：{error}")
                    if role == "baseline" and self.cold_low_memory_baseline_pending:
                        self.cold_low_memory_baseline_pending = False
                        self._show_no_realtime_snapshot(
                            "补扫失败：仍无实时文件快照，可稍后重试"
                        )
                    self._finish_scan_ui()
                    if self.closing:
                        self._finish_without_address_snapshot(
                            f"close_scan_failed:{error}"
                        )
                    elif role != "baseline":
                        self._start_pending_baseline()
                elif kind == "migration_advice_done":
                    self._render_migration_advice(message[1])
                elif kind == "migration_advice_error":
                    if (
                        self.migration_window is not None
                        and self.migration_window.winfo_exists()
                    ):
                        self.migration_refresh_button.configure(state=tk.NORMAL)
                        self.migration_status_var.set("建议刷新失败")
                        messagebox.showerror(
                            "迁移建议不可用",
                            message[1],
                            parent=self.migration_window,
                        )
                elif kind == "close_done":
                    self.logger.info(
                        "scan_ui_finished role=closing outcome=success snapshot_id=%s",
                        message[1].snapshot_id,
                    )
                    self.session_finished = True
                    self.progress.stop()
                    self.status_var.set(
                        f"本次会话已保存：{self._session_change_text(message[3])}"
                    )
                    self.destroy_after_id = self.root.after(
                        250, self._destroy_root
                    )
        except queue.Empty:
            pass
        except Exception as error:
            self._record_ui_error("后台消息处理", error)
        finally:
            try:
                if self.root.winfo_exists():
                    self.poll_after_id = self.root.after(100, self._poll_messages)
            except tk.TclError:
                pass

    def _start_pending_baseline(self) -> None:
        if (
            self.baseline_pending
            and self.session_start_snapshot_id is None
            and not self.closing
            and self.run_mode == RUN_MODE_FULL
        ):
            self.baseline_after_id = self.root.after(
                200,
                lambda: self._start_scan(
                    role="baseline", path=self.session_root_path
                ),
            )

    def _show_progress(self, progress: ScanProgress) -> None:
        display_path = progress.current_path
        if len(display_path) > 72:
            display_path = "…" + display_path[-71:]
        self.status_var.set(
            f"已扫描 {progress.file_count:,} 个文件 / "
            f"{format_bytes(progress.bytes_seen)} / 错误 {progress.error_count} · "
            f"{display_path}"
        )

    def _finish_scan_ui(self) -> None:
        self.progress.stop()
        self.active_scan_role = None
        self.pending_scan_note = None
        controls_disabled = self.closing or self.run_mode == RUN_MODE_LOW_MEMORY
        self.scan_button.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        self.save_snapshot_button.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        self.migration_advice_button.configure(
            state=(
                tk.NORMAL
                if self.current_result is not None and not self.closing
                else tk.DISABLED
            )
        )
        self.search_filter_button.configure(
            state=(
                tk.NORMAL
                if self.current_result is not None and not self.closing
                else tk.DISABLED
            )
        )
        self.choose_directory_button.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        self.cancel_button.configure(state=tk.DISABLED)
        self.path_entry.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        if not self.closing:
            self._refresh_breadcrumbs()
        self._update_tray_state()

    @staticmethod
    def _result_space_text(result: ScanResult) -> str:
        parts = [f"逻辑 {format_bytes(result.total_bytes)}"]
        if result.measurement_state == "exact":
            assert result.allocated_total_bytes is not None
            assert result.unique_allocated_total_bytes is not None
            parts.extend(
                (
                    f"分配 {format_bytes(result.allocated_total_bytes)}",
                    "唯一分配 "
                    f"{format_bytes(result.unique_allocated_total_bytes)}",
                )
            )
        elif result.measurement_state == "partial":
            parts.extend(
                (
                    "已测量分配 "
                    f"{format_bytes(result.measured_allocated_bytes)}",
                    "已确认唯一 "
                    f"{format_bytes(result.measured_unique_allocated_bytes)}",
                    "覆盖 "
                    f"{result.identity_measured_file_count:,}/"
                    f"{result.eligible_file_count:,} 文件",
                )
            )
        elif result.measurement_state == "unavailable":
            parts.append("分配/硬链接信息不可用")
        elif result.measurement_state == "legacy":
            parts.append("未记录分配/硬链接")
        return " · ".join(parts)

    @staticmethod
    def _item_space_text(item: ScanItem) -> str:
        parts = [f"逻辑 {format_bytes(item.size_bytes)}"]
        if item.allocated_size_bytes is not None:
            parts.append(f"分配 {format_bytes(item.allocated_size_bytes)}")
        if item.is_unique_owner is False:
            parts.append("同一文件的其他路径")
        elif item.unique_allocated_size_bytes is not None:
            parts.append(
                "唯一分配 "
                f"{format_bytes(item.unique_allocated_size_bytes)}"
            )
        if item.link_count is not None:
            parts.append(f"链接 {item.link_count:,}")
        if item.measurement_state == "partial":
            parts.append("原生信息不完整")
        elif item.measurement_state == "unavailable":
            parts.append("原生信息不可用")
        return " · ".join(parts)

    def _unique_growth_text(self, result: ScanResult) -> str:
        if result.measurement_state == "legacy" or result.snapshot_id is None:
            return ""
        previous_id = self.storage.previous_snapshot_id(result)
        if previous_id is None:
            return ""
        try:
            comparison = self.storage.compare_snapshot_accounting(
                result.snapshot_id, previous_id, limit=20
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            self.logger.exception("accounting_comparison_failed")
            return "唯一分配变化暂不可用"
        if not comparison["available"]:
            if comparison["old_measurement_state"] == "legacy":
                return "唯一分配变化不可比较（旧快照未记录该口径）"
            return "唯一分配变化不可比较（覆盖不足）"
        change = int(comparison["unique_allocated_total_change_bytes"])
        change_text = format_bytes(change)
        if change > 0:
            change_text = "+" + change_text
        text = f"唯一分配变化 {change_text}"
        unattributed = int(comparison["unattributed_unique_change_bytes"])
        if unattributed:
            unattributed_text = format_bytes(unattributed)
            if unattributed > 0:
                unattributed_text = "+" + unattributed_text
            text += f"（记录明细未归因 {unattributed_text}）"
        return text

    def _show_scan_result(
        self,
        result: ScanResult,
        growth: list[GrowthItem],
        has_baseline: bool,
        *,
        update_growth: bool = True,
    ) -> None:
        self.current_result = result
        self.migration_advice_button.configure(
            state=tk.DISABLED if self.closing else tk.NORMAL
        )
        self.search_filter_button.configure(
            state=tk.DISABLED if self.closing else tk.NORMAL
        )
        self.path_var.set(result.root_path)
        elapsed = (result.finished_at - result.started_at).total_seconds()
        unique_growth_text = self._unique_growth_text(result)
        accounting_suffix = (
            f"，{unique_growth_text}" if unique_growth_text else ""
        )
        self.status_var.set(
            f"扫描完成：{result.file_count:,} 个文件，"
            f"{self._result_space_text(result)}，{elapsed:.1f} 秒，"
            f"排除 {result.excluded_item_count}，错误 {result.error_count}"
            f"{accounting_suffix}"
        )
        data_time = result.finished_at.strftime("%Y-%m-%d %H:%M:%S")
        self.last_scan_summary = (
            f"{result.file_count:,} 文件 / {result.directory_count:,} 目录 / "
            f"排除 {result.excluded_item_count} / 错误 {result.error_count} / "
            f"{elapsed:.1f} 秒 / {data_time}"
        )
        self.detail_var.set(
            f"{result.root_path} · {self._result_space_text(result)} · "
            f"{result.file_count:,} 个文件"
            + (
                f" · 已排除 {result.excluded_rule_count} 条规则"
                if result.excluded_rule_count
                else ""
            )
            + (f" · {unique_growth_text}" if unique_growth_text else "")
        )
        self._draw_treemap(animate=True)
        self.logger.info(
            "treemap_rendered rectangles=%s path=%s",
            len(self.rectangle_items),
            result.root_path,
        )
        self._refresh_context_status()
        self.history_loaded = False
        if update_growth:
            if self.change_context is not None:
                self._refresh_change_view()
            else:
                self._fill_growth_tree(growth, has_baseline)

    def _set_default_change_context(
        self, context: tuple | None, subtitle: str, baseline_info: str
    ) -> None:
        self.default_change_context = context
        self.default_growth_subtitle = subtitle
        self.default_baseline_info = baseline_info
        if self.manual_baseline_mode is None:
            self._set_change_context(context, subtitle, baseline_info)
        else:
            self._apply_manual_baseline_mode()

    def _set_change_context(
        self, context: tuple | None, subtitle: str, baseline_info: str
    ) -> None:
        self.change_context = context
        self.growth_subtitle_var.set(subtitle)
        self.baseline_info_var.set(baseline_info)
        self._update_snapshot_summary()
        self._refresh_context_status()
        if self.latest_disk_sample is not None:
            self._update_runtime_change(self.latest_disk_sample)
        if context is None:
            self._fill_growth_tree([], False)
        else:
            self._refresh_change_view()

    def _automatic_baseline_mode(self) -> str:
        if (
            self.latest_full_snapshot_id is not None
            and self.automatic_baseline_snapshot_id
            == self.latest_full_snapshot_id
        ):
            return "latest_full"
        return "startup"

    def _configure_baseline_selector(self) -> None:
        values = [BASELINE_MODE_LABELS["startup"]]
        if self.latest_full_snapshot_id is not None:
            values.append(BASELINE_MODE_LABELS["latest_full"])
        self.baseline_mode_selector.configure(values=values, state="readonly")
        effective_mode = self.manual_baseline_mode or self._automatic_baseline_mode()
        if effective_mode == "latest_full" and self.latest_full_snapshot_id is None:
            effective_mode = "startup"
        self.baseline_mode_var.set(BASELINE_MODE_LABELS[effective_mode])

    def _select_baseline_mode(self, _event: tk.Event | None = None) -> None:
        selected_label = self.baseline_mode_var.get()
        selected_mode = next(
            (
                mode
                for mode, label in BASELINE_MODE_LABELS.items()
                if label == selected_label
            ),
            None,
        )
        if selected_mode is None:
            return
        self.manual_baseline_mode = selected_mode
        self._apply_manual_baseline_mode()
        self.status_var.set(f"已切换检测基线：{selected_label}")

    def _apply_manual_baseline_mode(self) -> None:
        if self.automatic_current_snapshot_id is None:
            return
        mode = self.manual_baseline_mode
        if mode == "latest_full":
            baseline_id = self.latest_full_snapshot_id
            baseline = (
                self.storage.get_snapshot_info(baseline_id)
                if baseline_id is not None
                else None
            )
            if baseline is None:
                self.manual_baseline_mode = "startup"
                self._configure_baseline_selector()
                self._apply_manual_baseline_mode()
                return
            baseline_info = (
                f"基线：最近完整保存（{baseline.finished_at:%Y-%m-%d %H:%M}）"
            )
            subtitle = "自最近完整保存以来的文件变化"
        else:
            baseline_id = self.session_start_snapshot_id
            baseline_info = "基线：本次启动快照"
            subtitle = "本次运行期间的文件变化"
        if baseline_id is None:
            return
        self.baseline_mode_var.set(BASELINE_MODE_LABELS[mode or "startup"])
        self._set_change_context(
            (
                "snapshots",
                self.automatic_current_snapshot_id,
                baseline_id,
                self.session_root_path,
            ),
            subtitle,
            baseline_info,
        )

    def _apply_startup_baseline(self, result: ScanResult) -> None:
        if result.snapshot_id is None:
            self._set_default_change_context(
                None,
                "启动快照未能保存",
                "基线：未建立",
            )
            self._show_scan_result(result, [], False, update_growth=False)
            return
        previous_id = continuous_baseline_snapshot_id(
            self.startup_previous_session, result.root_path
        )
        previous_info = (
            self.storage.get_snapshot_info(previous_id) if previous_id else None
        )
        self.automatic_current_snapshot_id = result.snapshot_id
        latest_full = self.storage.latest_full_snapshot_for_path(result.root_path)
        self.latest_full_snapshot_id = latest_full.id if latest_full else None
        if previous_info is not None:
            self.automatic_baseline_snapshot_id = previous_info.id
            baseline_info = (
                "基线：上次完整保存（"
                f"{previous_info.finished_at:%Y-%m-%d %H:%M}）"
            )
            subtitle = "自上次完整保存以来的文件变化"
        else:
            self.automatic_baseline_snapshot_id = result.snapshot_id
            baseline_info = "基线：本次启动快照（已建立，关闭时对比）"
            subtitle = "本次启动已建立新基线"
        self.manual_baseline_mode = None
        self._configure_baseline_selector()
        context = (
            "snapshots",
            result.snapshot_id,
            self.automatic_baseline_snapshot_id,
            result.root_path,
        )
        self._set_default_change_context(context, subtitle, baseline_info)
        self.logger.info(
            "baseline_applied snapshot_id=%s automatic_baseline_id=%s "
            "latest_full_id=%s",
            result.snapshot_id,
            self.automatic_baseline_snapshot_id,
            self.latest_full_snapshot_id,
        )
        self._show_scan_result(result, [], False, update_growth=False)

    def _apply_cold_low_memory_baseline(self, result: ScanResult) -> None:
        if result.snapshot_id is None:
            self._show_no_realtime_snapshot("本次补扫未能保存文件基线")
            return
        self.automatic_current_snapshot_id = result.snapshot_id
        self.automatic_baseline_snapshot_id = result.snapshot_id
        latest_full = self.storage.latest_full_snapshot_for_path(result.root_path)
        self.latest_full_snapshot_id = latest_full.id if latest_full else None
        self.manual_baseline_mode = None
        self._configure_baseline_selector()
        context = (
            "snapshots",
            result.snapshot_id,
            result.snapshot_id,
            result.root_path,
        )
        self._set_default_change_context(
            context,
            "此前只有磁盘口径记录，无文件地址明细；本次建立新基线",
            "基线：切回全功能模式时新建",
        )
        self.low_memory_origin = None
        self._show_scan_result(result, [], False, update_growth=False)
        self.status_var.set("已切回全功能模式并建立新的文件基线")

    def _apply_low_memory_resume(
        self,
        result: ScanResult,
        growth: list[GrowthItem],
        reference_snapshot_id: int | None,
    ) -> None:
        if result.snapshot_id is None:
            self._show_no_realtime_snapshot("补扫未能保存文件快照")
            return
        self.automatic_current_snapshot_id = result.snapshot_id
        latest_full = self.storage.latest_full_snapshot_for_path(result.root_path)
        self.latest_full_snapshot_id = latest_full.id if latest_full else None
        reference = (
            self.storage.get_snapshot_info(reference_snapshot_id)
            if reference_snapshot_id is not None
            else None
        )
        self.manual_baseline_mode = None
        if reference is None:
            self.automatic_baseline_snapshot_id = result.snapshot_id
            context = (
                "snapshots",
                result.snapshot_id,
                result.snapshot_id,
                result.root_path,
            )
            self._set_default_change_context(
                context,
                "此前无同路径快照，本次建立新基线",
                "基线：本次补扫新建",
            )
            self._show_scan_result(result, [], False, update_growth=False)
            self.status_var.set("补扫完成；此前无同路径快照，本次建立新基线")
        else:
            self.automatic_baseline_snapshot_id = reference.id
            self._set_default_change_context(
                (
                    "snapshots",
                    result.snapshot_id,
                    reference.id,
                    result.root_path,
                ),
                f"自参考快照（{reference.finished_at:%Y-%m-%d %H:%M}）以来的文件变化",
                f"基线：低内存前参考快照（{reference.finished_at:%Y-%m-%d %H:%M}）",
            )
            self._show_scan_result(
                result, growth, True, update_growth=False
            )
            self.status_var.set("补扫完成；文件变化按低内存前参考快照计算")
        self._configure_baseline_selector()
        self.low_memory_origin = None

    def _update_change_context_after_scan(
        self, result: ScanResult, previous_id: int | None
    ) -> None:
        if result.snapshot_id is None:
            return
        same_session_root = self._normalize_path(
            result.root_path
        ) == self._normalize_path(self.session_root_path)
        if same_session_root and self.automatic_baseline_snapshot_id is not None:
            self.automatic_current_snapshot_id = result.snapshot_id
            context = (
                "snapshots",
                result.snapshot_id,
                self.automatic_baseline_snapshot_id,
                result.root_path,
            )
            self._set_default_change_context(
                context,
                "自自动基线以来的文件变化",
                self.default_baseline_info,
            )
        elif previous_id is not None:
            self.manual_baseline_mode = None
            self.baseline_mode_var.set("当前目录快照对比")
            self.baseline_mode_selector.configure(state=tk.DISABLED)
            previous = self.storage.get_snapshot_info(previous_id)
            baseline_info = (
                f"基线：该路径上一快照（{previous.finished_at:%Y-%m-%d %H:%M}）"
                if previous
                else "基线：该路径上一快照"
            )
            self._set_change_context(
                (
                    "snapshots",
                    result.snapshot_id,
                    previous_id,
                    result.root_path,
                ),
                "同一路径最近两次扫描的文件变化量",
                baseline_info,
            )
        else:
            self.manual_baseline_mode = None
            self.baseline_mode_var.set("当前目录首次快照")
            self.baseline_mode_selector.configure(state=tk.DISABLED)
            self._set_change_context(
                None,
                "首次扫描：已建立该路径基线",
                "基线：该路径首次快照",
            )

    def _restore_default_baseline(self) -> None:
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self._show_no_realtime_snapshot(
                "低内存模式：无实时快照对比"
            )
            self.status_var.set("低内存模式未加载实时文件快照")
            return
        self.manual_baseline_mode = None
        self._configure_baseline_selector()
        self._set_change_context(
            self.default_change_context,
            self.default_growth_subtitle,
            self.default_baseline_info,
        )
        self.status_var.set("已恢复启动时自动判定的基线")

    @staticmethod
    def _format_elapsed(start: datetime, end: datetime) -> str:
        seconds = max(int((end - start).total_seconds()), 0)
        days, seconds = divmod(seconds, 86_400)
        hours, seconds = divmod(seconds, 3_600)
        minutes, _ = divmod(seconds, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if minutes or not parts:
            parts.append(f"{minutes}分钟")
        return "".join(parts)

    def _update_snapshot_summary(self) -> None:
        context = self.change_context
        if context is None or context[0] != "snapshots":
            self.snapshot_total_var.set("--")
            self.snapshot_total_label.configure(fg=COLORS["muted"])
            self.comparison_info_var.set("当前比较：尚无可用快照对")
            return
        new_info = self.storage.get_snapshot_info(context[1])
        old_info = self.storage.get_snapshot_info(context[2])
        if new_info is None or old_info is None:
            self.snapshot_total_var.set("快照数据已过期")
            self.snapshot_total_label.configure(fg=COLORS["muted"])
            self.comparison_info_var.set("当前比较：快照不存在或已清理")
            return
        change = new_info.total_bytes - old_info.total_bytes
        prefix = "+" if change > 0 else ""
        self.snapshot_total_var.set(f"{prefix}{format_bytes(change)}")
        color = (
            COLORS["positive"]
            if change > 0
            else "#2563eb" if change < 0 else COLORS["text"]
        )
        self.snapshot_total_label.configure(fg=color)
        self.comparison_info_var.set(
            "当前比较："
            f"{old_info.finished_at:%Y-%m-%d %H:%M:%S} → "
            f"{new_info.finished_at:%Y-%m-%d %H:%M:%S} "
            f"（相隔 {self._format_elapsed(old_info.finished_at, new_info.finished_at)}）"
            f" · {new_info.root_path}"
        )

    def _show_blind_spot(self) -> None:
        result = self.blind_spot_result
        if result is None or result.previous_at is None:
            self.blind_spot_var.set("无上次采样，无法判断未监控期间变化")
            self.blind_spot_label.configure(fg=COLORS["muted"])
            self._refresh_context_status()
            return
        change = result.change_bytes or 0
        prefix = "+" if change > 0 else ""
        if not result.is_significant:
            self.blind_spot_var.set(
                f"变化 {prefix}{format_bytes(change)}，未超过 100 MB 提示阈值"
            )
            self.blind_spot_label.configure(fg=COLORS["muted"])
            self._refresh_context_status()
            return
        elapsed_seconds = max(result.elapsed.total_seconds(), 0) if result.elapsed else 0
        if elapsed_seconds >= 86_400:
            elapsed_text = f"{elapsed_seconds / 86_400:.1f} 天"
        elif elapsed_seconds >= 3_600:
            elapsed_text = f"{elapsed_seconds / 3_600:.1f} 小时"
        else:
            elapsed_text = "不足 1 小时"
        direction = "增长" if change > 0 else "减少"
        self.blind_spot_var.set(
            f"⚠ 期间未监控 {elapsed_text}，{direction} "
            f"{prefix}{format_bytes(change)}（无目录明细）"
        )
        self.blind_spot_label.configure(
            fg="#d97706" if change > 0 else "#15803d"
        )
        self._refresh_context_status()

    def _refresh_context_status(self) -> None:
        baseline = self.baseline_info_var.get().replace("基线：", "", 1)
        blind = "等待采样"
        result = self.blind_spot_result
        if result is not None:
            if result.previous_at is None:
                blind = "无上次采样"
            elif result.is_significant:
                change = result.change_bytes or 0
                prefix = "+" if change > 0 else ""
                blind = f"需关注 {prefix}{format_bytes(change)}"
            else:
                blind = "未超过 100 MB"
        skeleton = self.navigation_skeleton
        skeleton_text = "未建立"
        if skeleton is not None:
            memory_mb = skeleton.estimated_bytes / (1024 * 1024)
            if skeleton.degraded:
                budget_mb = NAVIGATION_MEMORY_BUDGET_BYTES / (1024 * 1024)
                skeleton_text = (
                    f"精简 {len(skeleton.nodes):,} 目录 / 骨架估算 {memory_mb:.1f} MB"
                    f"（文件明细按 {budget_mb:.0f} MB 预算精简）"
                )
            else:
                skeleton_text = (
                    f"完整 {len(skeleton.nodes):,} 目录 / "
                    f"骨架估算 {memory_mb:.1f} MB"
                )
        self.context_status_var.set(
            f"模式：{RUN_MODE_LABELS[self.run_mode].replace('模式', '')} · "
            f"基线：{baseline} · 扫描：{self.last_scan_summary} · "
            f"导航骨架：{skeleton_text} · 盲区：{blind}"
        )

    def _on_tab_changed(self, _event: tk.Event | None = None) -> None:
        if self.notebook.select() == str(self.history_tab) and not self.history_loaded:
            self._load_snapshot_history(reset=True)

    def _load_more_snapshots(self) -> None:
        self._load_snapshot_history(reset=False)

    def _load_snapshot_history(self, *, reset: bool) -> None:
        if reset:
            self.history_cursor = None
            self.history_items_by_id.clear()
            for item_id in self.history_tree.get_children():
                self.history_tree.delete(item_id)
        try:
            source_label = self.history_source_filter_var.get()
            source = next(
                (
                    key
                    for key, label in SNAPSHOT_SOURCE_LABELS.items()
                    if label == source_label
                ),
                None,
            )

            def parse_date(value: str, *, end_of_day: bool) -> datetime | None:
                stripped = value.strip()
                if not stripped:
                    return None
                parsed = datetime.fromisoformat(stripped)
                if len(stripped) == 10 and end_of_day:
                    parsed = parsed.replace(hour=23, minute=59, second=59)
                return parsed

            snapshots = self.storage.list_snapshots(
                limit=200,
                cursor=self.history_cursor,
                root_path=self.history_path_filter_var.get().strip() or None,
                source=source,
                finished_after=parse_date(
                    self.history_after_filter_var.get(), end_of_day=False
                ),
                finished_before=parse_date(
                    self.history_before_filter_var.get(), end_of_day=True
                ),
            )
        except ValueError as error:
            self.history_status_var.set(f"筛选无效：{error}")
            messagebox.showerror("快照筛选无效", str(error), parent=self.root)
            return
        for snapshot in snapshots:
            item_id = f"snapshot-{snapshot.id}"
            self.history_items_by_id[item_id] = snapshot
            tags = ("manual",) if snapshot.source == "manual_save" else ()
            self.history_tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    f"{snapshot.finished_at:%Y-%m-%d %H:%M:%S}",
                    snapshot.root_path,
                    format_bytes(snapshot.total_bytes),
                    SNAPSHOT_SOURCE_LABELS.get(snapshot.source, snapshot.source),
                    snapshot.note,
                ),
                tags=tags,
            )
        if snapshots:
            last = snapshots[-1]
            self.history_cursor = (last.finished_at, last.id)
        self.history_loaded = True
        self.load_more_button.configure(
            state=tk.NORMAL if len(snapshots) == 200 else tk.DISABLED
        )
        self.history_status_var.set(
            (
                f"已加载 {len(self.history_items_by_id):,} 条快照；"
                "请选择两条同路径记录"
                if self.history_items_by_id
                else "尚无快照；完成首次扫描后会在这里显示历史记录"
            )
        )
        self._update_history_selection()

    def _toggle_history_selection(self, event: tk.Event) -> str | None:
        if event.state & 0x0005:
            return None
        item_id = self.history_tree.identify_row(event.y)
        if not item_id:
            return None
        selected = list(self.history_tree.selection())
        if item_id in selected:
            self.history_tree.selection_remove(item_id)
        elif len(selected) < 2:
            self.history_tree.selection_add(item_id)
        else:
            self.history_tree.selection_set(item_id)
        self.history_tree.focus(item_id)
        self.root.after_idle(self._update_history_selection)
        return "break"

    def _update_history_selection(
        self, _event: tk.Event | None = None
    ) -> None:
        selected = self.history_tree.selection()
        self.view_history_button.configure(
            state=tk.NORMAL if len(selected) == 1 else tk.DISABLED
        )
        if len(selected) != 2:
            self.compare_history_button.configure(state=tk.DISABLED)
            self.history_status_var.set(
                f"已选择 {len(selected)}/2 条；普通单击可选择任意两项"
            )
            return
        first = self.history_items_by_id.get(selected[0])
        second = self.history_items_by_id.get(selected[1])
        if first is None or second is None:
            self.compare_history_button.configure(state=tk.DISABLED)
            return
        if self._normalize_path(first.root_path) != self._normalize_path(
            second.root_path
        ):
            self.compare_history_button.configure(state=tk.DISABLED)
            self.history_status_var.set("所选快照路径不同，不能进行对比")
            return
        self.compare_history_button.configure(state=tk.NORMAL)
        self.history_status_var.set("已选择两条同路径快照，可以开始对比")

    def _compare_selected_snapshots(self) -> None:
        selected = self.history_tree.selection()
        if len(selected) != 2:
            return
        snapshots = [self.history_items_by_id[item_id] for item_id in selected]
        old_info, new_info = sorted(
            snapshots, key=lambda item: (item.finished_at, item.id)
        )
        if old_info.id == new_info.id or self._normalize_path(
            old_info.root_path
        ) != self._normalize_path(new_info.root_path):
            self._update_history_selection()
            return
        try:
            config_comparison = self.storage.snapshot_config_comparison(
                new_info.id, old_info.id
            )
        except ValueError as error:
            messagebox.showerror("快照不可比较", str(error), parent=self.root)
            return
        if config_comparison["status"] == "mismatch":
            differences = "、".join(config_comparison["differences"])
            self.history_status_var.set(
                f"扫描配置不同，已阻止增长归因：{differences}"
            )
            messagebox.showwarning(
                "扫描配置不同",
                f"默认不生成增长归因。不同项：{differences}",
                parent=self.root,
            )
            return
        self._set_change_context(
            (
                "snapshots",
                new_info.id,
                old_info.id,
                new_info.root_path,
            ),
            (
                "历史快照对比，不代表本次低内存期间变化"
                if self.run_mode == RUN_MODE_LOW_MEMORY
                else "指定历史快照的文件变化"
            ),
            f"基线：手动选择（{old_info.finished_at:%Y-%m-%d %H:%M}）",
        )
        self.manual_baseline_mode = None
        self.baseline_mode_var.set("自定义历史对比")
        self.baseline_mode_selector.configure(state=tk.DISABLED)
        self.notebook.select(self.changes_tab)
        self.status_var.set("已切换到所选历史快照对比")

    def _fill_growth_tree(
        self, growth: list[GrowthItem], has_baseline: bool
    ) -> None:
        direction_name = "增长" if self.change_view_var.get() == "增长" else "减少"
        self.growth_tree.heading("change", text=direction_name)
        self.growth_item_by_id.clear()
        for item_id in self.growth_tree.get_children():
            self.growth_tree.delete(item_id)
        if not has_baseline:
            self.growth_tree.insert("", tk.END, text="首次扫描：已建立基线")
            return
        if not growth:
            self.growth_tree.insert(
                "", tk.END, text=f"未发现{direction_name}项目"
            )
            return
        for node in build_growth_tree(growth):
            self._insert_growth_node("", node, open_node=True)

    def _insert_growth_node(
        self, parent_id: str, node: GrowthTreeNode, *, open_node: bool = False
    ) -> None:
        item = node.item
        if item is None:
            label = f"↳ {self._middle_ellipsis(node.label)}"
        else:
            marker = "📁" if item.kind == "directory" else "📄"
            label = (
                f"{marker} {self._middle_ellipsis(node.label)}"
                f"{self._directory_change_hint(item)}"
            )
        change_text = (
            format_bytes(node.change_bytes)
            if node.change_bytes is not None
            else f"含 {len(node.children)} 项"
        )
        item_id = self.growth_tree.insert(
            parent_id,
            tk.END,
            text=label,
            values=(
                format_bytes(node.current_bytes)
                if node.current_bytes is not None
                else "",
                change_text,
            ),
            open=open_node,
        )
        if item is not None:
            self.growth_item_by_id[item_id] = item
        for child in node.children:
            self._insert_growth_node(item_id, child)

    @staticmethod
    def _middle_ellipsis(text: str, max_chars: int = 38) -> str:
        if len(text) <= max_chars:
            return text
        side = max((max_chars - 1) // 2, 1)
        return f"{text[:side]}…{text[-side:]}"

    def _hover_growth_item(self, event: tk.Event) -> None:
        item_id = self.growth_tree.identify_row(event.y)
        if not item_id:
            return
        item = self.growth_item_by_id.get(item_id)
        if item is not None:
            self.growth_hover_var.set(
                f"{item.path} · 变化 {format_bytes(item.change_bytes)}"
            )
        else:
            self.growth_hover_var.set(self.growth_tree.item(item_id, "text"))

    def _leave_growth_tree(self, _event: tk.Event | None = None) -> None:
        self.growth_hover_var.set("悬停项目查看完整路径")

    def _resize_growth_columns(self, event: tk.Event) -> None:
        tree_width = max(event.width, 360)
        self.growth_tree.column("#0", width=max(tree_width - 210, 180))

    def _directory_change_hint(self, item: GrowthItem) -> str:
        if item.kind != "directory":
            return ""
        root_path = self.change_context[-1] if self.change_context else ""
        try:
            relative_parts = Path(item.path).relative_to(root_path).parts
        except (ValueError, TypeError):
            return "（双击继续定位）"
        if len(relative_parts) >= 2:
            return "（深层有变化，双击继续定位）"
        return "（双击继续定位）"

    def _refresh_change_view(self, _event: tk.Event | None = None) -> None:
        if self.change_context is None:
            return
        direction = "increase" if self.change_view_var.get() == "增长" else "decrease"
        context_kind = self.change_context[0]
        if context_kind == "session":
            session_id = self.change_context[1]
            changes = self.storage.get_session_growth(
                session_id, direction=direction
            )
            has_baseline = bool(self.change_context[2])
        else:
            new_snapshot_id, old_snapshot_id = self.change_context[1:3]
            root_path = self.change_context[-1]
            try:
                deep_comparison = ReadOnlyDatabase(
                    self.storage.database_path
                ).compare_directory_history(
                    new_snapshot_id,
                    old_snapshot_id,
                    root_path,
                    direction=direction,
                    limit=100,
                )
            except ControlError as error:
                if error.code not in {
                    "scan_config_unknown",
                    "directory_history_unavailable",
                }:
                    self.status_var.set(f"快照对比失败：{error}")
                    self._fill_growth_tree([], False)
                    return
                changes = self.storage.compare_snapshot_changes(
                    new_snapshot_id,
                    old_snapshot_id,
                    direction=direction,
                )
            else:
                changes = [
                    GrowthItem(
                        path=(
                            item["path"]
                            or os.path.join(root_path, "未记录文件明细")
                        ),
                        parent_path=root_path,
                        name=item["name"],
                        kind=item["kind"],
                        old_size_bytes=item["old_size_bytes"],
                        new_size_bytes=item["new_size_bytes"],
                    )
                    for item in deep_comparison["items"]
                ]
            has_baseline = True
        self._fill_growth_tree(changes, has_baseline)

    def _session_change_text(self, sample: DiskSample) -> str:
        if self.session_start_sample is None:
            return "已保存最终磁盘用量"
        change = sample.used_bytes - self.session_start_sample.used_bytes
        prefix = "+" if change > 0 else ""
        return f"{prefix}{format_bytes(change)}"

    def _direct_items(self) -> list[ScanItem]:
        if self.current_result is None:
            return []
        return sorted(
            (
                item
                for item in self.current_result.items
                if item.parent_path == self.current_result.root_path
                and item.path != self.current_result.root_path
                and item.size_bytes > 0
            ),
            key=lambda item: item.size_bytes,
            reverse=True,
        )

    def _schedule_treemap_resize(self, _event: tk.Event | None = None) -> None:
        if self.closing:
            return
        if self.treemap_resize_after_id is not None:
            try:
                self.root.after_cancel(self.treemap_resize_after_id)
            except tk.TclError:
                pass
        self.treemap_resize_after_id = self.root.after(
            150, self._draw_resized_treemap
        )

    def _draw_resized_treemap(self) -> None:
        self.treemap_resize_after_id = None
        self._draw_treemap(animate=False)

    def _cancel_treemap_animation(self) -> None:
        if self.treemap_animation_after_id is not None:
            try:
                self.root.after_cancel(self.treemap_animation_after_id)
            except tk.TclError:
                pass
            self.treemap_animation_after_id = None

    def _draw_treemap(self, *, animate: bool = False) -> None:
        canvas = self.map_canvas
        self._cancel_treemap_animation()
        old_positions = {
            item.path: (x, y, width, height)
            for x, y, width, height, item in self.rectangle_items
        }
        canvas.delete("all")
        self.rectangle_items.clear()
        self.rectangle_canvas_ids.clear()
        self.hovered_rectangle_path = None
        width = max(canvas.winfo_width(), 2)
        height = max(canvas.winfo_height(), 2)
        items = self._direct_items()
        if not items:
            canvas.create_text(
                width / 2,
                height / 2,
                text=self.treemap_placeholder_text,
                fill=COLORS["muted"],
                font=self.fonts["body_emphasis"],
                width=max(width - 30, 20),
            )
            return
        target_rectangles = layout_rectangles(
            items, 3, 3, width - 6, height - 6
        )
        drawable_rectangles = [
            rectangle
            for rectangle in target_rectangles
            if rectangle[2] >= 1 and rectangle[3] >= 1
        ]
        should_animate = (
            animate
            and len(drawable_rectangles) <= self.MAX_ANIMATED_TREEMAP_ITEMS
            and width > 20
            and height > 20
        )
        animations: list[tuple] = []
        current_rectangles: list[
            tuple[float, float, float, float, ScanItem]
        ] = []
        for index, (x, y, rect_width, rect_height, item) in enumerate(
            drawable_rectangles
        ):
            if item.kind == "directory":
                fill = COLORS["directory"] if index % 2 == 0 else COLORS["directory_alt"]
            else:
                fill = COLORS["file"]
            if should_animate:
                start = old_positions.get(item.path)
                if start is None:
                    start = (
                        x + rect_width / 2,
                        y + rect_height / 2,
                        1.0,
                        1.0,
                    )
            else:
                start = (x, y, rect_width, rect_height)
            start_x, start_y, start_width, start_height = start
            rectangle_id = canvas.create_rectangle(
                start_x,
                start_y,
                start_x + start_width,
                start_y + start_height,
                fill=fill,
                outline="#ffffff",
                width=2,
            )
            self.rectangle_canvas_ids[item.path] = rectangle_id
            text_id: int | None = None
            if rect_width > 72 and rect_height > 34:
                label = f"{item.name}\n{format_bytes(item.size_bytes)}"
                text_id = canvas.create_text(
                    start_x + 7,
                    start_y + 6,
                    text=label,
                    anchor=tk.NW,
                    width=max(rect_width - 14, 10),
                    fill="#172033",
                    font=self.fonts["tiny_bold"],
                )
            animations.append(
                (
                    rectangle_id,
                    text_id,
                    start,
                    (x, y, rect_width, rect_height),
                    item,
                )
            )
            current_rectangles.append(
                (start_x, start_y, start_width, start_height, item)
            )
        self.rectangle_items = current_rectangles
        if not should_animate:
            self.rectangle_items = drawable_rectangles
            return

        def animate_frame(frame: int = 1) -> None:
            self.treemap_animation_after_id = None
            progress = min(frame / self.TREEMAP_ANIMATION_FRAMES, 1.0)
            eased = 1 - (1 - progress) ** 3
            current: list[tuple[float, float, float, float, ScanItem]] = []
            try:
                for rectangle_id, text_id, start, target, item in animations:
                    values = tuple(
                        start[index]
                        + (target[index] - start[index]) * eased
                        for index in range(4)
                    )
                    current_x, current_y, current_width, current_height = values
                    canvas.coords(
                        rectangle_id,
                        current_x,
                        current_y,
                        current_x + current_width,
                        current_y + current_height,
                    )
                    if text_id is not None:
                        canvas.coords(text_id, current_x + 7, current_y + 6)
                    current.append((*values, item))
            except tk.TclError:
                return
            self.rectangle_items = current
            if frame < self.TREEMAP_ANIMATION_FRAMES and not self.closing:
                self.treemap_animation_after_id = self.root.after(
                    16, lambda: animate_frame(frame + 1)
                )
            else:
                self.rectangle_items = drawable_rectangles

        animate_frame()

    def _select_rectangle(self, event: tk.Event) -> None:
        item = treemap_item_at(self.rectangle_items, event.x, event.y)
        if item:
            kind = "目录" if item.kind == "directory" else "文件"
            self.detail_var.set(
                f"{kind}：{item.path} · {self._item_space_text(item)} · "
                f"{item.file_count:,} 个文件"
            )

    def _hover_rectangle(self, event: tk.Event) -> None:
        item = treemap_item_at(self.rectangle_items, event.x, event.y)
        path = item.path if item is not None else None
        if path == self.hovered_rectangle_path:
            return
        if self.hovered_rectangle_path is not None:
            old_id = self.rectangle_canvas_ids.get(self.hovered_rectangle_path)
            if old_id is not None:
                self.map_canvas.itemconfigure(old_id, outline="#ffffff", width=2)
        self.hovered_rectangle_path = path
        if item is None:
            return
        rectangle_id = self.rectangle_canvas_ids.get(item.path)
        if rectangle_id is not None:
            self.map_canvas.itemconfigure(
                rectangle_id, outline=COLORS["accent"], width=3
            )
        kind = {
            "directory": "目录",
            "file": "文件",
            "aggregate": "小文件汇总",
        }.get(item.kind, item.kind)
        self.detail_var.set(
            f"{kind}：{item.path} · {self._item_space_text(item)} · "
            f"{item.file_count:,} 个文件"
        )

    def _leave_treemap(self, _event: tk.Event | None = None) -> None:
        if self.hovered_rectangle_path is not None:
            rectangle_id = self.rectangle_canvas_ids.get(
                self.hovered_rectangle_path
            )
            if rectangle_id is not None:
                self.map_canvas.itemconfigure(
                    rectangle_id, outline="#ffffff", width=2
                )
        self.hovered_rectangle_path = None
        if self.current_result is not None:
            self.detail_var.set(
                f"{self.current_result.root_path} · "
                f"{self._result_space_text(self.current_result)} · "
                f"{self.current_result.file_count:,} 个文件"
            )

    def _open_rectangle(self, event: tk.Event) -> None:
        item = treemap_item_at(self.rectangle_items, event.x, event.y)
        if item and item.kind == "directory" and os.path.isdir(item.path):
            self._navigate_to(item.path)

    def _select_growth_item(self, _event: tk.Event) -> None:
        selected = self.growth_tree.selection()
        if not selected:
            return
        item = self.growth_item_by_id.get(selected[0])
        if item is not None:
            self.detail_var.set(
                f"{item.path} · 变化 {format_bytes(item.change_bytes)}"
            )

    def _open_growth_item(self, event: tk.Event) -> None:
        item_id = self.growth_tree.identify_row(event.y)
        item = self.growth_item_by_id.get(item_id)
        if item is None or item.kind != "directory":
            return
        if not os.path.isdir(item.path):
            self.status_var.set("该目录已不存在，无法继续扫描")
            return
        self.notebook.select(self.distribution_tab)
        self._navigate_to(item.path)

    def _draw_trend(self) -> None:
        canvas = self.trend_canvas
        canvas.delete("all")
        self.trend_marker_positions.clear()
        width = max(canvas.winfo_width(), 2)
        height = max(canvas.winfo_height(), 2)
        geometry = build_trend_geometry(
            self.latest_samples,
            self.session_boundaries,
            now=datetime.now(),
            hours=self.trend_hours,
            width=width,
            height=height,
            max_points=self.TREND_MAX_POINTS,
            gap_threshold=self.TREND_GAP_THRESHOLD,
        )
        self.trend_window_start = geometry.window_start
        self.trend_window_end = geometry.window_end
        if not geometry.has_samples:
            canvas.create_text(
                width / 2,
                height / 2,
                text="正在建立趋势数据……",
                fill=COLORS["muted"],
                font=self.fonts["caption"],
            )
            return
        padding_x = 12
        padding_y = 12
        assert geometry.low is not None
        assert geometry.high is not None
        for gap_start, gap_end in geometry.gap_rectangles:
            canvas.create_rectangle(
                gap_start,
                padding_y,
                gap_end,
                height - padding_y,
                fill="#fed7aa",
                outline="",
                stipple="gray50",
            )

        for marker in geometry.markers:
            self.trend_marker_positions.append((marker.x, marker.boundary))
            canvas.create_line(
                marker.x,
                padding_y,
                marker.x,
                height - padding_y,
                fill="#94a3b8",
                dash=(2, 3),
            )

        for points in geometry.segments:
            if len(points) == 1:
                x, y = points[0]
                canvas.create_oval(
                    x - 2,
                    y - 2,
                    x + 2,
                    y + 2,
                    fill=COLORS["accent"],
                    outline="",
                )
            else:
                flat_points = [
                    coordinate for point in points for coordinate in point
                ]
                canvas.create_line(
                    *flat_points, fill=COLORS["accent"], width=2
                )
        canvas.create_text(
            padding_x,
            3,
            anchor=tk.NW,
            text=f"最高 {format_bytes(geometry.high)}",
            fill=COLORS["muted"],
            font=self.fonts["tiny"],
        )
        canvas.create_text(
            width - padding_x,
            height - 3,
            anchor=tk.SE,
            text=f"最低 {format_bytes(geometry.low)}",
            fill=COLORS["muted"],
            font=self.fonts["tiny"],
        )

    def _hover_trend(self, event: tk.Event) -> None:
        if self.trend_window_start is None or self.trend_window_end is None:
            return
        if self.trend_marker_positions:
            x, marker = min(
                self.trend_marker_positions,
                key=lambda entry: abs(entry[0] - event.x),
            )
            if abs(x - event.x) <= 5:
                marker_name = "会话开始" if marker.kind == "start" else "会话结束"
                self.trend_hover_var.set(
                    f"{marker_name} · {marker.occurred_at:%m-%d %H:%M:%S}"
                )
                return
        width = max(self.trend_canvas.winfo_width(), 2)
        padding_x = 12
        fraction = (event.x - padding_x) / max(width - 2 * padding_x, 1)
        fraction = max(0.0, min(fraction, 1.0))
        target = self.trend_window_start + (
            self.trend_window_end - self.trend_window_start
        ) * fraction
        sample = nearest_disk_sample(self.latest_samples, target)
        if sample is not None:
            self.trend_hover_var.set(
                f"{sample.recorded_at:%m-%d %H:%M:%S} · "
                f"已用 {format_bytes(sample.used_bytes)}"
            )

    def _leave_trend(self, _event: tk.Event | None = None) -> None:
        self.trend_hover_var.set("悬停趋势线查看精确数值")

    def _begin_close_snapshot(self) -> None:
        if self.session_finished:
            self._destroy_root()
            return
        if self.session_id is None:
            self._destroy_root()
            return
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            self._finish_without_address_snapshot("low_memory_close")
            return
        if self.close_mode == "quick":
            self._finish_without_address_snapshot("quick_close")
            return
        if self.session_start_snapshot_id is None:
            self._finish_without_address_snapshot("normal_close_without_baseline")
            return
        self._start_scan(role="closing", path=self.session_root_path)

    def _finish_without_address_snapshot(self, reason: str) -> None:
        try:
            if self.session_id is not None:
                final_sample = read_disk_sample(self.session_root_path)
                self.storage.add_disk_sample(final_sample)
                self.storage.finish_session(
                    self.session_id,
                    final_sample,
                    end_reason=reason,
                )
        finally:
            self.session_finished = True
            self._destroy_root()

    def _destroy_root(self) -> None:
        for attribute in (
            "poll_after_id",
            "sample_after_id",
            "baseline_after_id",
            "destroy_after_id",
            "control_after_id",
            "automation_after_id",
            "treemap_animation_after_id",
            "treemap_resize_after_id",
            "display_after_id",
        ):
            callback_id = getattr(self, attribute, None)
            if callback_id is not None:
                try:
                    self.root.after_cancel(callback_id)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        self._close_settings_window()
        self._close_migration_advice()
        self._close_snapshot_browser()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                self.logger.exception("tray_stop_failed")
            self.tray_icon = None
        if self.control_bridge is not None:
            try:
                self.control_bridge.stop()
            except Exception:
                self.logger.exception("control_server_stop_failed")
            self.control_bridge = None
        self.navigation_skeleton = None
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass
        if self.log_handler is not None:
            self.logger.info("app_stopped")
            self.logger.removeHandler(self.log_handler)
            self.log_handler.close()
            self.log_handler = None

    def _finalize_on_process_exit(self) -> None:
        if self.session_id is None or self.session_finished:
            return
        try:
            final_sample = read_disk_sample(self.session_root_path)
            self.storage.add_disk_sample(final_sample)
            latest_snapshot_id: int | None = None
            if (
                self.current_result is not None
                and self.current_result.root_path
                == os.path.normcase(os.path.abspath(self.session_root_path))
                and self.current_result.snapshot_id
                != self.session_start_snapshot_id
            ):
                latest_snapshot_id = self.current_result.snapshot_id
            self.storage.finish_session(
                self.session_id,
                final_sample,
                end_snapshot_id=latest_snapshot_id,
                end_reason="process_exit",
            )
            self.session_finished = True
        except Exception:
            # 进程退出或系统关机阶段不再弹窗；未完成会话将在下次启动补记。
            self.logger.exception("process_exit_finalize_failed")

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.run_mode != RUN_MODE_LOW_MEMORY:
            close_behavior = self.storage.get_setting("close_behavior", "ask")
            if close_behavior == "ask":
                dialog = CloseChoiceDialog(self.root)
                if dialog.choice is None:
                    return
                self.close_mode = dialog.choice
                if dialog.remember:
                    self.storage.set_setting("close_behavior", dialog.choice)
            else:
                self.close_mode = close_behavior
        self._begin_close_sequence()

    def _request_controlled_close(self, behavior: str) -> None:
        if self.closing:
            return
        if behavior not in {"full", "quick"}:
            raise ValueError("关闭方式必须是 full 或 quick")
        self.close_mode = behavior
        self._begin_close_sequence()

    def _begin_close_sequence(self) -> None:
        self.closing = True
        self._close_settings_window()
        self._close_migration_advice()
        self._close_snapshot_browser()
        self.mode_toggle_button.configure(state=tk.DISABLED)
        self._cancel_treemap_animation()
        if self.treemap_resize_after_id is not None:
            try:
                self.root.after_cancel(self.treemap_resize_after_id)
            except tk.TclError:
                pass
            self.treemap_resize_after_id = None
        self.baseline_pending = False
        self.scan_button.configure(state=tk.DISABLED)
        self.save_snapshot_button.configure(state=tk.DISABLED)
        self.migration_advice_button.configure(state=tk.DISABLED)
        self.search_filter_button.configure(state=tk.DISABLED)
        self.up_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.DISABLED)
        self.path_entry.configure(state=tk.DISABLED)
        if self.run_mode == RUN_MODE_LOW_MEMORY:
            if self.scan_thread and self.scan_thread.is_alive():
                self.cancel_event.set()
                self.status_var.set("正在停止意外残留的扫描并退出低内存模式……")
                return
            self._finish_without_address_snapshot("low_memory_close")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            if self.active_scan_role == "baseline" and self.close_mode == "full":
                self.status_var.set(
                    "正在完成启动基线，随后会保存关闭快照并自动退出……"
                )
            else:
                self.cancel_event.set()
                self.status_var.set("正在停止当前扫描并准备关闭快照……")
            return
        self._begin_close_snapshot()


def main() -> None:
    awareness_status = enable_per_monitor_dpi_awareness()
    root = tk.Tk()
    initial_path = os.environ.get("DISK_GROWTH_MONITOR_INITIAL_PATH", "C:\\")
    app = DiskMonitorApp(root, initial_path=initial_path, enable_tray=True)
    app.logger.info("dpi_awareness status=%s", awareness_status)
    root.mainloop()
