from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from tkinter import messagebox, ttk

from .automation import AutoModeConfig, normalize_process_names
from .exclusions import compile_exclusion_rules


@dataclass(frozen=True)
class SettingsSubmission:
    """Validated values collected by the settings dialog before persistence."""

    close_behavior: str
    run_mode: str
    autostart_enabled: bool
    collect_file_space: bool
    exclude_rules: tuple[str, ...]
    auto_mode_config: AutoModeConfig


@dataclass(frozen=True)
class SettingsDialogState:
    close_behavior_label: str
    run_mode_label: str
    autostart_enabled: bool
    collect_file_space: bool
    exclude_rules: tuple[str, ...]
    auto_mode_config: AutoModeConfig


def _key_for_label(
    labels: Mapping[str, str], selected_label: str, field_name: str
) -> str:
    for key, label in labels.items():
        if label == selected_label:
            return key
    raise ValueError(f"{field_name}选项无效")


def validate_settings_submission(
    *,
    close_behavior_label: str,
    run_mode_label: str,
    autostart_enabled: bool,
    collect_file_space: bool,
    exclude_rules_text: str,
    auto_enabled: bool,
    process_names_text: str,
    memory_pressure_enabled: bool,
    high_percent_text: str,
    low_percent_text: str,
    resume_rescan_label: str,
    session_root_path: str,
    close_behavior_labels: Mapping[str, str],
    run_mode_labels: Mapping[str, str],
    auto_rescan_labels: Mapping[str, str],
) -> SettingsSubmission:
    """Validate dialog text without mutating the app, storage, or Windows settings."""

    exclude_rules = tuple(
        line.strip() for line in exclude_rules_text.splitlines() if line.strip()
    )
    compile_exclusion_rules(session_root_path, list(exclude_rules))
    auto_mode_config = AutoModeConfig(
        enabled=auto_enabled,
        process_names=normalize_process_names(process_names_text),
        memory_pressure_enabled=memory_pressure_enabled,
        high_percent=int(high_percent_text.strip()),
        low_percent=int(low_percent_text.strip()),
        resume_rescan=_key_for_label(
            auto_rescan_labels, resume_rescan_label, "恢复补扫策略"
        ),
    ).validate()
    return SettingsSubmission(
        close_behavior=_key_for_label(
            close_behavior_labels, close_behavior_label, "关闭方式"
        ),
        run_mode=_key_for_label(run_mode_labels, run_mode_label, "运行模式"),
        autostart_enabled=autostart_enabled,
        collect_file_space=collect_file_space,
        exclude_rules=exclude_rules,
        auto_mode_config=auto_mode_config,
    )


class SettingsDialog:
    """Owns the settings form while the application owns lifecycle and writes."""

    def __init__(
        self,
        window: tk.Toplevel,
        *,
        state: SettingsDialogState,
        session_root_path: str,
        from_tray: bool,
        panel_background: str,
        close_behavior_labels: Mapping[str, str],
        run_mode_labels: Mapping[str, str],
        auto_rescan_labels: Mapping[str, str],
        on_submit: Callable[[SettingsSubmission], bool],
        on_close: Callable[[], None],
    ) -> None:
        self.window = window
        self.state = state
        self.session_root_path = session_root_path
        self.from_tray = from_tray
        self.panel_background = panel_background
        self.close_behavior_labels = close_behavior_labels
        self.run_mode_labels = run_mode_labels
        self.auto_rescan_labels = auto_rescan_labels
        self.on_submit = on_submit
        self.on_close = on_close

    def build(self) -> ttk.Combobox:
        header = ttk.Frame(self.window, padding=(22, 18, 22, 12))
        header.pack(fill=tk.X)
        ttk.Label(header, text="应用设置", style="DialogTitle.TLabel").pack(
            anchor=tk.W
        )
        ttk.Label(
            header,
            text=(
                "独立调整运行方式与自动低内存策略"
                if self.from_tray
                else "调整运行方式与自动低内存策略"
            ),
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(3, 0))

        body = ttk.Frame(self.window, padding=(22, 0, 22, 0))
        body.pack(fill=tk.BOTH, expand=True)
        general_panel = ttk.Frame(body, style="Panel.TFrame", padding=16)
        general_panel.pack(fill=tk.X)
        general_panel.columnconfigure(1, weight=1)
        ttk.Label(
            general_panel,
            text="常规",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 12))
        ttk.Label(
            general_panel,
            text="关闭主窗口时",
            background=self.panel_background,
        ).grid(row=1, column=0, sticky=tk.W, padx=(0, 18), pady=4)
        behavior_var = tk.StringVar(value=self.state.close_behavior_label)
        behavior_box = ttk.Combobox(
            general_panel,
            textvariable=behavior_var,
            values=tuple(self.close_behavior_labels.values()),
            state="readonly",
            width=26,
        )
        behavior_box.grid(row=1, column=1, sticky=tk.EW, pady=4)

        ttk.Label(
            general_panel,
            text="当前运行模式",
            background=self.panel_background,
        ).grid(row=2, column=0, sticky=tk.W, padx=(0, 18), pady=4)
        run_mode_var = tk.StringVar(value=self.state.run_mode_label)
        ttk.Combobox(
            general_panel,
            textvariable=run_mode_var,
            values=tuple(self.run_mode_labels.values()),
            state="readonly",
            width=26,
        ).grid(row=2, column=1, sticky=tk.EW, pady=4)

        autostart_var = tk.BooleanVar(value=self.state.autostart_enabled)
        ttk.Checkbutton(
            general_panel,
            text="登录 Windows 后自动启动监控器",
            variable=autostart_var,
            style="Panel.TCheckbutton",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        file_space_var = tk.BooleanVar(value=self.state.collect_file_space)
        ttk.Checkbutton(
            general_panel,
            text="扫描时读取分配大小与硬链接（精确但明显更慢）",
            variable=file_space_var,
            style="Panel.TCheckbutton",
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Label(
            general_panel,
            text="默认关闭；只影响保存设置后的新扫描",
            style="PanelMuted.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(3, 0))
        ttk.Label(
            general_panel,
            text="扫描排除规则",
            background=self.panel_background,
        ).grid(row=6, column=0, sticky=tk.NW, padx=(0, 18), pady=(12, 4))
        exclude_rules_text = tk.Text(
            general_panel,
            width=34,
            height=4,
            wrap="none",
            relief=tk.SOLID,
            borderwidth=1,
        )
        exclude_rules_text.grid(row=6, column=1, sticky=tk.EW, pady=(12, 4))
        exclude_rules_text.insert("1.0", "\n".join(self.state.exclude_rules))
        ttk.Label(
            general_panel,
            text="逐行填写扫描根内绝对路径或相对 glob；默认不排除任何目录",
            style="PanelMuted.TLabel",
        ).grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(0, 2))

        automation_panel = ttk.Frame(body, style="Panel.TFrame", padding=16)
        automation_panel.pack(fill=tk.X, pady=(12, 0))
        automation_panel.columnconfigure(1, weight=1)
        ttk.Label(
            automation_panel,
            text="自动低内存模式",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))
        auto_enabled_var = tk.BooleanVar(value=self.state.auto_mode_config.enabled)
        ttk.Checkbutton(
            automation_panel,
            text="检测指定进程或内存压力后自动切换",
            variable=auto_enabled_var,
            style="Panel.TCheckbutton",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(
            automation_panel,
            text="监控进程",
            background=self.panel_background,
        ).grid(row=2, column=0, sticky=tk.W, padx=(0, 18), pady=(12, 4))
        process_names_var = tk.StringVar(
            value=";".join(self.state.auto_mode_config.process_names)
        )
        ttk.Entry(
            automation_panel,
            textvariable=process_names_var,
            width=34,
        ).grid(row=2, column=1, sticky=tk.EW, pady=(12, 4))
        ttk.Label(
            automation_panel,
            text="用分号分隔，可省略 .exe",
            style="PanelMuted.TLabel",
        ).grid(row=3, column=1, sticky=tk.W)
        memory_pressure_var = tk.BooleanVar(
            value=self.state.auto_mode_config.memory_pressure_enabled
        )
        ttk.Checkbutton(
            automation_panel,
            text="使用系统内存压力作为兜底触发条件",
            variable=memory_pressure_var,
            style="Panel.TCheckbutton",
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 4))
        threshold_row = ttk.Frame(automation_panel, style="Panel.TFrame")
        threshold_row.grid(row=5, column=0, columnspan=2, sticky=tk.W)
        high_percent_var = tk.StringVar(
            value=str(self.state.auto_mode_config.high_percent)
        )
        low_percent_var = tk.StringVar(
            value=str(self.state.auto_mode_config.low_percent)
        )
        ttk.Label(
            threshold_row, text="进入阈值", background=self.panel_background
        ).pack(side=tk.LEFT)
        ttk.Entry(
            threshold_row, textvariable=high_percent_var, width=5
        ).pack(side=tk.LEFT, padx=(6, 3))
        ttk.Label(
            threshold_row, text="%    恢复阈值", background=self.panel_background
        ).pack(side=tk.LEFT)
        ttk.Entry(
            threshold_row, textvariable=low_percent_var, width=5
        ).pack(side=tk.LEFT, padx=(6, 3))
        ttk.Label(
            threshold_row, text="%", background=self.panel_background
        ).pack(side=tk.LEFT)
        ttk.Label(
            automation_panel,
            text="恢复全功能后",
            background=self.panel_background,
        ).grid(row=6, column=0, sticky=tk.W, padx=(0, 18), pady=(12, 0))
        resume_rescan_var = tk.StringVar(
            value=self.auto_rescan_labels[
                self.state.auto_mode_config.resume_rescan
            ]
        )
        ttk.Combobox(
            automation_panel,
            textvariable=resume_rescan_var,
            values=tuple(self.auto_rescan_labels.values()),
            state="readonly",
            width=26,
        ).grid(row=6, column=1, sticky=tk.EW, pady=(12, 0))

        footer = ttk.Frame(self.window, padding=(22, 12, 22, 18))
        footer.pack(fill=tk.X)
        ttk.Label(
            footer,
            text="分钟采样保留 30 天 · 目录快照保留 90 天",
            style="Subtitle.TLabel",
        ).pack(side=tk.LEFT, pady=(5, 0))
        buttons = ttk.Frame(footer)
        buttons.pack(side=tk.RIGHT)

        def save() -> None:
            try:
                submission = validate_settings_submission(
                    close_behavior_label=behavior_var.get(),
                    run_mode_label=run_mode_var.get(),
                    autostart_enabled=autostart_var.get(),
                    collect_file_space=file_space_var.get(),
                    exclude_rules_text=exclude_rules_text.get("1.0", "end"),
                    auto_enabled=auto_enabled_var.get(),
                    process_names_text=process_names_var.get(),
                    memory_pressure_enabled=memory_pressure_var.get(),
                    high_percent_text=high_percent_var.get(),
                    low_percent_text=low_percent_var.get(),
                    resume_rescan_label=resume_rescan_var.get(),
                    session_root_path=self.session_root_path,
                    close_behavior_labels=self.close_behavior_labels,
                    run_mode_labels=self.run_mode_labels,
                    auto_rescan_labels=self.auto_rescan_labels,
                )
            except ValueError as error:
                messagebox.showerror("设置无效", str(error), parent=self.window)
                return
            if self.on_submit(submission):
                self.on_close()

        ttk.Button(buttons, text="取消", command=self.on_close).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存", command=save).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        self.window.bind("<Return>", lambda _event: save())
        return behavior_box
