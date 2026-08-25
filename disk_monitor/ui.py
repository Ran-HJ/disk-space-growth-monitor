from __future__ import annotations

import atexit
import gc
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .agent_control import GuiAgentController
from .autostart import is_autostart_enabled, set_autostart
from .control_bridge import GuiControlBridge
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
from .navigation import (
    NAVIGATION_MEMORY_BUDGET_BYTES,
    materialize_navigation_result,
    merge_directory_skeleton,
)
from .scanner import ScanCancelled, scan_path
from .service import (
    calculate_blind_spot,
    continuous_baseline_snapshot_id,
    downsample_disk_samples,
    find_sample_gaps,
    nearest_disk_sample,
    normalize_drive,
    read_disk_sample,
    split_sample_segments,
)
from .storage import Storage


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
            font=("Microsoft YaHei UI", 11, "bold"),
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
    ) -> None:
        self.root = root
        self.storage = storage or Storage()
        stored_run_mode = self.storage.get_setting("run_mode", RUN_MODE_FULL)
        self.run_mode = (
            stored_run_mode
            if stored_run_mode in RUN_MODE_LABELS
            else RUN_MODE_FULL
        )
        if stored_run_mode not in RUN_MODE_LABELS:
            self.storage.set_setting("run_mode", self.run_mode)
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
        self.low_memory_reference_finished_at: datetime | None = None
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
        self.treemap_animation_after_id: str | None = None
        self.treemap_resize_after_id: str | None = None
        self.nav_stack = self._path_chain(requested_path)
        self.nav_cache: dict[str, ScanResult] = {}
        self.nav_invalidated_roots: set[str] = set()

        self.path_var = tk.StringVar(value=requested_path)
        self.status_var = tk.StringVar(value="准备就绪")
        self.mode_status_var = tk.StringVar(value=RUN_MODE_LABELS[self.run_mode])
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
        self.trend_range_var = tk.StringVar(value="24 小时")
        self.trend_title_var = tk.StringVar(value="最近 24 小时已用空间")
        self.trend_hover_var = tk.StringVar(value="悬停趋势线查看精确数值")
        self.growth_hover_var = tk.StringVar(value="悬停项目查看完整路径")
        self.context_status_var = tk.StringVar(
            value="基线：正在建立 · 扫描：尚无数据 · 盲区：等待采样"
        )
        self.last_scan_summary = "尚无数据"
        self.treemap_placeholder_text = "点击“重新扫描当前目录”生成空间分布图"

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
        self.root.geometry("1180x780")
        self.root.minsize(900, 680)
        self.root.configure(bg=COLORS["background"])
        bundle_root = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
        )
        icon_path = bundle_root / "assets" / "app.ico"
        if icon_path.exists():
            self.root.iconbitmap(default=str(icon_path))

    def _configure_styles(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "MetricName.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "MetricValue.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "ChangeIncrease.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["positive"],
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "ChangeDecrease.TLabel",
            background=COLORS["panel"],
            foreground="#15803d",
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

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
            font=("Microsoft YaHei UI", 11, "bold"),
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
            font=("Microsoft YaHei UI", 10, "bold"),
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
            font=("Microsoft YaHei UI", 11, "bold"),
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
            font=("Microsoft YaHei UI", 9, "bold"),
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
            font=("Microsoft YaHei UI", 11, "bold"),
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
            font=("Microsoft YaHei UI", 10, "bold"),
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
            font=("Microsoft YaHei UI", 10, "bold"),
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
            font=("Microsoft YaHei UI", 10, "bold"),
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
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        self.compare_history_button = ttk.Button(
            history_header,
            text="对比所选两项",
            command=self._compare_selected_snapshots,
            state=tk.DISABLED,
        )
        self.compare_history_button.pack(side=tk.RIGHT)
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
            f"{result.root_path} · {format_bytes(result.total_bytes)} · "
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

    def _select_low_memory_reference(
        self,
    ) -> tuple[int | None, datetime | None]:
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
                return info.id, info.finished_at
        return None, None

    def _request_run_mode(self, requested_mode: str) -> bool:
        if requested_mode not in RUN_MODE_LABELS:
            raise ValueError(f"未知运行模式：{requested_mode}")
        if requested_mode == self.run_mode:
            return True
        if self.session_id is None or self.session_start_sample is None:
            messagebox.showinfo(
                "正在初始化",
                "请等待首次磁盘采样完成后再切换运行模式。",
                parent=self.root,
            )
            return False
        if requested_mode == RUN_MODE_LOW_MEMORY:
            if self.active_scan_role is not None or (
                self.scan_thread and self.scan_thread.is_alive()
            ):
                messagebox.showinfo(
                    "扫描进行中",
                    "请等待当前扫描结束，或先取消扫描，再切换低内存模式。",
                    parent=self.root,
                )
                return False
            self._enter_low_memory_mode()
            return True
        return self._leave_low_memory_mode()

    def _toggle_run_mode(self) -> None:
        requested_mode = (
            RUN_MODE_LOW_MEMORY
            if self.run_mode == RUN_MODE_FULL
            else RUN_MODE_FULL
        )
        self._request_run_mode(requested_mode)

    def _enter_low_memory_mode(self) -> None:
        reference_id, reference_finished_at = self._select_low_memory_reference()
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
        self.low_memory_reference_finished_at = reference_finished_at
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

    def _leave_low_memory_mode(
        self, *, should_scan: bool | None = None
    ) -> bool:
        cold_start = self.low_memory_origin == "cold"
        if cold_start:
            if should_scan is None:
                messagebox.showinfo(
                    "建立文件基线",
                    "此前只有磁盘口径记录，无文件地址明细；本次建立新基线。",
                    parent=self.root,
                )
                should_scan = True
        elif should_scan is None:
            answer = messagebox.askyesnocancel(
                "切回全功能模式",
                "是否立即扫描当前监控路径并重建空间分布？\n\n"
                "是：立即补扫；否：只切换模式，稍后手动扫描；取消：保持低内存模式。",
                parent=self.root,
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

    def _open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("设置")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        body = ttk.Frame(window, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="关闭行为",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        current_behavior = self.storage.get_setting("close_behavior", "ask")
        behavior_var = tk.StringVar(
            value=CLOSE_BEHAVIOR_LABELS.get(current_behavior, "每次询问")
        )
        behavior_box = ttk.Combobox(
            body,
            textvariable=behavior_var,
            values=tuple(CLOSE_BEHAVIOR_LABELS.values()),
            state="readonly",
            width=24,
        )
        behavior_box.grid(row=1, column=0, sticky=tk.W)

        ttk.Label(
            body,
            text="运行模式",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=2, column=0, sticky=tk.W, pady=(16, 6))
        run_mode_var = tk.StringVar(value=RUN_MODE_LABELS[self.run_mode])
        run_mode_box = ttk.Combobox(
            body,
            textvariable=run_mode_var,
            values=tuple(RUN_MODE_LABELS.values()),
            state="readonly",
            width=24,
        )
        run_mode_box.grid(row=3, column=0, sticky=tk.W)

        autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        ttk.Checkbutton(
            body,
            text="登录 Windows 后自动启动监控器",
            variable=autostart_var,
        ).grid(row=4, column=0, sticky=tk.W, pady=(16, 4))
        ttk.Label(
            body,
            text="分钟采样保留 30 天，原始目录快照保留 90 天。",
            foreground=COLORS["muted"],
        ).grid(row=5, column=0, sticky=tk.W, pady=(4, 16))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, sticky=tk.E)

        def save_settings() -> None:
            behavior = next(
                key
                for key, label in CLOSE_BEHAVIOR_LABELS.items()
                if label == behavior_var.get()
            )
            requested_mode = next(
                key
                for key, label in RUN_MODE_LABELS.items()
                if label == run_mode_var.get()
            )
            if not self._request_run_mode(requested_mode):
                return
            try:
                self.storage.set_setting("close_behavior", behavior)
                set_autostart(autostart_var.get())
            except OSError as error:
                messagebox.showerror("设置失败", str(error), parent=window)
                return
            self.status_var.set("设置已保存")
            window.destroy()

        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存", command=save_settings).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        window.protocol("WM_DELETE_WINDOW", window.destroy)

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
                    self.low_memory_reference_finished_at = None
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
            )
            note = self.pending_scan_note if role == "manual_save" else None
            snapshot_source = "manual" if role == "low_memory_resume" else role
            self.storage.save_scan(result, note=note, source=snapshot_source)
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
                if kind == "scan_progress":
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
        self.choose_directory_button.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        self.cancel_button.configure(state=tk.DISABLED)
        self.path_entry.configure(
            state=tk.DISABLED if controls_disabled else tk.NORMAL
        )
        if not self.closing:
            self._refresh_breadcrumbs()

    def _show_scan_result(
        self,
        result: ScanResult,
        growth: list[GrowthItem],
        has_baseline: bool,
        *,
        update_growth: bool = True,
    ) -> None:
        self.current_result = result
        self.path_var.set(result.root_path)
        elapsed = (result.finished_at - result.started_at).total_seconds()
        self.status_var.set(
            f"扫描完成：{result.file_count:,} 个文件，"
            f"{format_bytes(result.total_bytes)}，{elapsed:.1f} 秒，"
            f"跳过/错误 {result.error_count}"
        )
        data_time = result.finished_at.strftime("%Y-%m-%d %H:%M:%S")
        self.last_scan_summary = (
            f"{result.file_count:,} 文件 / {result.directory_count:,} 目录 / "
            f"错误 {result.error_count} / {elapsed:.1f} 秒 / {data_time}"
        )
        self.detail_var.set(
            f"{result.root_path} · {format_bytes(result.total_bytes)} · "
            f"{result.file_count:,} 个文件"
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
        snapshots = self.storage.list_snapshots(
            limit=200, cursor=self.history_cursor
        )
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
            changes = self.storage.compare_snapshot_changes(
                new_snapshot_id,
                old_snapshot_id,
                direction=direction,
            )
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
                font=("Microsoft YaHei UI", 11),
                width=max(width - 30, 20),
            )
            return
        target_rectangles: list[
            tuple[float, float, float, float, ScanItem]
        ] = []
        self._layout_rectangles(
            items, 3, 3, width - 6, height - 6, target_rectangles
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
                    font=("Microsoft YaHei UI", 8, "bold"),
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

    def _layout_rectangles(
        self,
        items: list[ScanItem],
        x: float,
        y: float,
        width: float,
        height: float,
        output: list[tuple[float, float, float, float, ScanItem]] | None = None,
    ) -> None:
        target = self.rectangle_items if output is None else output
        if not items or width <= 0 or height <= 0:
            return
        if len(items) == 1:
            target.append((x, y, width, height, items[0]))
            return

        total = sum(item.size_bytes for item in items)
        split_target = total / 2
        accumulated = 0
        split_index = 1
        for index, item in enumerate(items[:-1], start=1):
            accumulated += item.size_bytes
            split_index = index
            if accumulated >= split_target:
                break
        first = items[:split_index]
        second = items[split_index:]
        first_total = sum(item.size_bytes for item in first)
        ratio = first_total / total if total else 0.5
        if width >= height:
            first_width = width * ratio
            self._layout_rectangles(first, x, y, first_width, height, target)
            self._layout_rectangles(
                second, x + first_width, y, width - first_width, height, target
            )
        else:
            first_height = height * ratio
            self._layout_rectangles(first, x, y, width, first_height, target)
            self._layout_rectangles(
                second, x, y + first_height, width, height - first_height, target
            )

    def _item_at(self, x: float, y: float) -> ScanItem | None:
        for left, top, width, height, item in reversed(self.rectangle_items):
            if left <= x <= left + width and top <= y <= top + height:
                return item
        return None

    def _select_rectangle(self, event: tk.Event) -> None:
        item = self._item_at(event.x, event.y)
        if item:
            kind = "目录" if item.kind == "directory" else "文件"
            self.detail_var.set(
                f"{kind}：{item.path} · {format_bytes(item.size_bytes)} · "
                f"{item.file_count:,} 个文件"
            )

    def _hover_rectangle(self, event: tk.Event) -> None:
        item = self._item_at(event.x, event.y)
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
            f"{kind}：{item.path} · {format_bytes(item.size_bytes)} · "
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
                f"{format_bytes(self.current_result.total_bytes)} · "
                f"{self.current_result.file_count:,} 个文件"
            )

    def _open_rectangle(self, event: tk.Event) -> None:
        item = self._item_at(event.x, event.y)
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
        samples = self.latest_samples
        if not samples:
            self.trend_window_start = None
            self.trend_window_end = None
            canvas.create_text(
                width / 2,
                height / 2,
                text="正在建立趋势数据……",
                fill=COLORS["muted"],
                font=("Microsoft YaHei UI", 9),
            )
            return
        padding_x = 12
        padding_y = 12
        window_end = datetime.now()
        window_start = window_end - timedelta(hours=self.trend_hours)
        self.trend_window_start = window_start
        self.trend_window_end = window_end
        seconds = max((window_end - window_start).total_seconds(), 1)

        def x_for(moment: datetime) -> float:
            fraction = (moment - window_start).total_seconds() / seconds
            return padding_x + max(0.0, min(fraction, 1.0)) * (
                width - 2 * padding_x
            )

        values = [sample.used_bytes for sample in samples]
        low = min(values)
        high = max(values)
        value_range = max(high - low, 1)

        gaps = find_sample_gaps(
            samples, gap_threshold=self.TREND_GAP_THRESHOLD
        )
        if len(gaps) > 500:
            step = len(gaps) / 500
            gaps = [gaps[int(index * step)] for index in range(500)]
        for gap_start, gap_end in gaps:
            canvas.create_rectangle(
                x_for(gap_start),
                padding_y,
                x_for(gap_end),
                height - padding_y,
                fill="#fed7aa",
                outline="",
                stipple="gray50",
            )

        for boundary in self.session_boundaries:
            x = x_for(boundary.occurred_at)
            self.trend_marker_positions.append((x, boundary))
            canvas.create_line(
                x,
                padding_y,
                x,
                height - padding_y,
                fill="#94a3b8",
                dash=(2, 3),
            )

        raw_segments = split_sample_segments(
            samples, gap_threshold=self.TREND_GAP_THRESHOLD
        )
        if len(raw_segments) > 500:
            step = len(raw_segments) / 500
            raw_segments = [
                raw_segments[int(index * step)] for index in range(500)
            ]
        total_samples = max(len(samples), 1)
        for segment in raw_segments:
            allowance = max(
                2,
                round(self.TREND_MAX_POINTS * len(segment) / total_samples),
            )
            drawn = downsample_disk_samples(segment, max_points=allowance)
            points = [
                (
                    x_for(sample.recorded_at),
                    height
                    - padding_y
                    - (sample.used_bytes - low)
                    * (height - 2 * padding_y)
                    / value_range,
                )
                for sample in drawn
            ]
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
            text=f"最高 {format_bytes(high)}",
            fill=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        canvas.create_text(
            width - padding_x,
            height - 3,
            anchor=tk.SE,
            text=f"最低 {format_bytes(low)}",
            fill=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
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
            "treemap_animation_after_id",
            "treemap_resize_after_id",
        ):
            callback_id = getattr(self, attribute, None)
            if callback_id is not None:
                try:
                    self.root.after_cancel(callback_id)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
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
    root = tk.Tk()
    initial_path = os.environ.get("DISK_GROWTH_MONITOR_INITIAL_PATH", "C:\\")
    DiskMonitorApp(root, initial_path=initial_path)
    root.mainloop()
