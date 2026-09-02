from __future__ import annotations

import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

from disk_monitor.models import DiskSample, GrowthItem, ScanResult
from disk_monitor.scanner import scan_path
from disk_monitor.service import BLIND_SPOT_THRESHOLD_BYTES, normalize_drive
from disk_monitor.storage import Storage
from disk_monitor.ui import BASELINE_MODE_LABELS, DiskMonitorApp
from disk_monitor.windows_display import DisplayMetrics, WorkArea


def pump_until(root: tk.Tk, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待界面操作完成超时")


def wait_for_destroy(root: tk.Tk, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not root.winfo_exists():
                return
            root.update()
        except tk.TclError:
            return
        time.sleep(0.01)
    raise AssertionError("等待窗口正常销毁超时")


def force_high_dpi_scaling(window: tk.Misc) -> DisplayMetrics:
    previous = float(window.tk.call("tk", "scaling"))
    target = 192 / 72
    window.tk.call("tk", "scaling", target)
    return DisplayMetrics(192, previous, target, abs(previous - target) >= 0.01)


class UiFlowTests(unittest.TestCase):
    def assert_widget_fully_visible(
        self, window: tk.Misc, widget: tk.Misc
    ) -> None:
        self.assertTrue(widget.winfo_ismapped())
        left = widget.winfo_rootx() - window.winfo_rootx()
        top = widget.winfo_rooty() - window.winfo_rooty()
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(left + widget.winfo_width(), window.winfo_width())
        self.assertLessEqual(top + widget.winfo_height(), window.winfo_height())

    def test_snapshot_browser_uses_same_deep_tree_and_largest_file_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            deep = scan_root / "branch" / "deep"
            deep.mkdir(parents=True)
            (deep / "data.bin").write_bytes(b"x" * 64)
            root = tk.Tk()
            root.withdraw()
            with patch(
                "disk_monitor.ui.sync_tk_scaling",
                side_effect=force_high_dpi_scaling,
            ):
                app = DiskMonitorApp(
                    root,
                    storage=Storage(base / "monitor.db"),
                    initial_path=str(scan_root),
                    log_path=base / "ui.log",
                )
            try:
                pump_until(
                    root,
                    lambda: app.current_result is not None
                    and app.active_scan_role is None,
                )
                work_area = (680, 380, WorkArea(0, 0, 1366, 768))
                with patch(
                    "disk_monitor.ui.cursor_work_area",
                    return_value=work_area,
                ), patch(
                    "disk_monitor.windows_display.cursor_work_area",
                    return_value=work_area,
                ):
                    app._open_current_snapshot_browser()
                    root.update()
                self.assertIsNotNone(app.snapshot_browser_window)
                assert app.snapshot_browser_window is not None

                buttons: dict[str, ttk.Button] = {}
                trees: list[ttk.Treeview] = []
                entries: list[ttk.Entry] = []
                search_mode: ttk.Combobox | None = None

                def collect(widget: tk.Misc) -> None:
                    nonlocal search_mode
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Button):
                            buttons[str(child.cget("text"))] = child
                        if isinstance(child, ttk.Treeview):
                            trees.append(child)
                        if isinstance(child, ttk.Entry):
                            entries.append(child)
                        if isinstance(child, ttk.Combobox) and "路径前缀" in tuple(
                            child.cget("values")
                        ):
                            search_mode = child
                        collect(child)

                collect(app.snapshot_browser_window)
                self.assertIn("搜索", buttons)
                self.assertIn("最大文件", buttons)
                self.assertIn("下一页", buttons)
                for text in ("上一级", "浏览当前层", "搜索", "最大文件", "下一页"):
                    self.assert_widget_fully_visible(
                        app.snapshot_browser_window, buttons[text]
                    )
                self.assertEqual(len(trees), 1)
                self.assertIsNotNone(search_mode)
                assert search_mode is not None
                self.assertEqual(search_mode.get(), "子串")
                browser_tree = trees[0]
                root_rows = browser_tree.get_children()
                self.assertTrue(root_rows)
                self.assertIn("branch", browser_tree.item(root_rows[0], "text"))

                buttons["最大文件"].invoke()
                root.update()
                largest_rows = browser_tree.get_children()
                self.assertTrue(largest_rows)
                self.assertIn("data.bin", browser_tree.item(largest_rows[0], "text"))

                query_entry = next(
                    entry
                    for entry in entries
                    if str(entry.cget("state")) != "readonly"
                )
                query_entry.insert(0, "data.bin")
                query_entry.event_generate("<Return>")
                root.update()
                search_rows = browser_tree.get_children()
                self.assertTrue(search_rows)
                self.assertIn("data.bin", browser_tree.item(search_rows[0], "text"))
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_migration_advice_window_is_read_only_and_has_no_action_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            root = tk.Tk()
            root.withdraw()
            with patch(
                "disk_monitor.ui.sync_tk_scaling",
                side_effect=force_high_dpi_scaling,
            ):
                app = DiskMonitorApp(
                    root,
                    storage=Storage(base / "monitor.db"),
                    initial_path=str(scan_root),
                    log_path=base / "ui.log",
                )
            try:
                pump_until(
                    root,
                    lambda: app.current_result is not None
                    and app.active_scan_role is None,
                )
                work_area = (680, 380, WorkArea(0, 0, 1366, 768))
                with patch(
                    "disk_monitor.ui.cursor_work_area",
                    return_value=work_area,
                ), patch(
                    "disk_monitor.windows_display.cursor_work_area",
                    return_value=work_area,
                ):
                    app._open_migration_advice()
                    root.update()
                self.assertIsNotNone(app.migration_window)
                assert app.migration_window is not None

                buttons: dict[str, ttk.Button] = {}

                def collect_buttons(widget: tk.Misc) -> None:
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Button):
                            buttons[str(child.cget("text"))] = child
                        collect_buttons(child)

                collect_buttons(app.migration_window)
                self.assertIn("选择目标盘", buttons)
                self.assertIn("刷新建议", buttons)
                self.assertIn("关闭", buttons)
                for text in ("选择目标盘", "刷新建议", "关闭"):
                    self.assert_widget_fully_visible(
                        app.migration_window, buttons[text]
                    )
                self.assertFalse(
                    {"开始迁移", "执行", "应用", "复制", "删除", "回滚"}
                    & buttons.keys()
                )
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_accounting_labels_distinguish_unrecorded_legacy_snapshot(self) -> None:
        result = ScanResult(
            root_path=r"C:\fixture",
            started_at=datetime.now(),
            finished_at=datetime.now(),
            total_bytes=1,
            file_count=1,
            directory_count=1,
            error_count=0,
            snapshot_id=2,
            allocated_total_bytes=4096,
            unique_allocated_total_bytes=4096,
            measurement_state="exact",
        )
        app = SimpleNamespace(
            storage=SimpleNamespace(
                previous_snapshot_id=lambda _result: 1,
                compare_snapshot_accounting=lambda *_args, **_kwargs: {
                    "available": False,
                    "old_measurement_state": "legacy",
                },
            )
        )

        self.assertIn(
            "未记录分配/硬链接",
            DiskMonitorApp._result_space_text(
                ScanResult(
                    root_path=r"C:\fixture",
                    started_at=datetime.now(),
                    finished_at=datetime.now(),
                    total_bytes=1,
                    file_count=1,
                    directory_count=1,
                    error_count=0,
                )
            ),
        )
        self.assertEqual(
            DiskMonitorApp._unique_growth_text(app, result),
            "唯一分配变化不可比较（旧快照未记录该口径）",
        )

    def test_exact_file_space_setting_is_explicitly_passed_to_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 4097)
            storage = Storage(base / "monitor.db")
            storage.set_setting("file_space_accounting", "exact")
            storage.set_setting("exclude_rules", "*.tmp")
            root = tk.Tk()
            root.withdraw()
            with patch(
                "disk_monitor.ui.scan_path", wraps=scan_path
            ) as scanner:
                app = DiskMonitorApp(
                    root,
                    storage=storage,
                    initial_path=str(scan_root),
                    log_path=base / "ui.log",
                )
                try:
                    pump_until(
                        root,
                        lambda: app.session_start_snapshot_id is not None
                        and app.active_scan_role is None,
                    )
                    self.assertTrue(app.collect_file_space)
                    self.assertTrue(
                        any(
                            call.kwargs.get("collect_file_space") is True
                            for call in scanner.call_args_list
                        )
                    )
                    self.assertTrue(
                        any(
                            call.kwargs.get("exclude_rules") == ("*.tmp",)
                            for call in scanner.call_args_list
                        )
                    )
                    self.assertIsNotNone(app.current_result)
                    assert app.current_result is not None
                    self.assertEqual(
                        app.current_result.measurement_state, "exact"
                    )
                    self.assertIn("唯一分配", app.detail_var.get())
                finally:
                    app.session_finished = True
                    app._destroy_root()

    def test_settings_dialog_persists_validated_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 16)
            storage = Storage(base / "monitor.db")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
                log_path=base / "ui.log",
            )
            try:
                pump_until(
                    root,
                    lambda: app.current_result is not None
                    and app.active_scan_role is None,
                )
                with patch("disk_monitor.ui.set_autostart") as set_autostart:
                    app._open_settings()
                    root.update()
                    assert app.settings_window is not None
                    text_widgets: list[tk.Text] = []
                    combo_boxes: list[ttk.Combobox] = []
                    buttons: dict[str, ttk.Button] = {}

                    def collect(widget: tk.Misc) -> None:
                        for child in widget.winfo_children():
                            if isinstance(child, tk.Text):
                                text_widgets.append(child)
                            if isinstance(child, ttk.Combobox):
                                combo_boxes.append(child)
                            if isinstance(child, ttk.Button):
                                buttons[str(child.cget("text"))] = child
                            collect(child)

                    collect(app.settings_window)
                    self.assertEqual(len(text_widgets), 1)
                    text_widgets[0].insert("1.0", "*.tmp\n")
                    behavior_box = next(
                        box
                        for box in combo_boxes
                        if "始终快速退出" in tuple(box.cget("values"))
                    )
                    behavior_box.set("始终快速退出")
                    buttons["保存"].invoke()
                    root.update()

                self.assertIsNone(app.settings_window)
                self.assertEqual(storage.get_setting("close_behavior"), "quick")
                self.assertEqual(storage.get_setting("exclude_rules"), "*.tmp")
                self.assertEqual(app.exclude_rules, ("*.tmp",))
                set_autostart.assert_called_once_with(False)
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_scan_and_full_close_complete_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "first.bin").write_bytes(b"a" * 16)
            (scan_root / "second-start.bin").write_bytes(b"b" * 8)
            storage = Storage(base / "monitor.db")
            storage.set_setting("close_behavior", "full")
            log_path = base / "ui.log"
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
                log_path=log_path,
            )
            try:
                self.assertEqual(len(app.notebook.tabs()), 3)
                root.deiconify()
                root.geometry("900x680")
                root.update()
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )
                self.assertGreaterEqual(len(app.rectangle_items), 2)
                self.assertIsNotNone(app.navigation_skeleton)
                root.withdraw()
                item_id = app.growth_tree.insert("", tk.END, text="测试目录")
                app.growth_item_by_id[item_id] = GrowthItem(
                    path=str(scan_root),
                    parent_path=str(scan_root.parent),
                    name=scan_root.name,
                    kind="directory",
                    old_size_bytes=0,
                    new_size_bytes=1,
                )
                app.notebook.select(app.changes_tab)
                with patch.object(
                    app.growth_tree, "identify_row", return_value=item_id
                ), patch.object(app, "_navigate_to") as navigate:
                    app._open_growth_item(SimpleNamespace(y=0))
                self.assertEqual(
                    app.notebook.select(), str(app.distribution_tab)
                )
                navigate.assert_called_once_with(str(scan_root))
                (scan_root / "second.bin").write_bytes(b"b" * 32)

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)

                session = storage.latest_completed_session()
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.status, "completed")
                self.assertEqual(session.end_reason, "normal_close")
                self.assertIsNotNone(session.start_snapshot_id)
                self.assertIsNotNone(session.end_snapshot_id)
                self.assertIn(
                    "scan_ui_finished role=closing outcome=success "
                    f"snapshot_id={session.end_snapshot_id}",
                    log_path.read_text(encoding="utf-8"),
                )
                self.assertIn("本次启动快照", app.baseline_info_var.get())
                self.assertEqual(
                    app.blind_spot_var.get(),
                    "无上次采样，无法判断未监控期间变化",
                )
                self.assertIsNone(app.treemap_animation_after_id)
                self.assertIsNone(app.treemap_resize_after_id)
                self.assertIsNone(app.navigation_skeleton)

            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_deep_navigation_uses_skeleton_and_trend_range_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            deep = scan_root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            (deep / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
            storage.set_setting("close_behavior", "quick")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
            )
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )
                self.assertIsNotNone(app.navigation_skeleton)

                root.deiconify()
                minimum_width, minimum_height = root.minsize()
                root.geometry(f"{minimum_width}x{minimum_height}")
                root.update()
                controls = app.scan_button.master
                self.assertLessEqual(
                    max(
                        child.winfo_x() + child.winfo_width()
                        for child in controls.winfo_children()
                    ),
                    controls.winfo_width(),
                )
                self.assertTrue(app.map_canvas.winfo_ismapped())
                self.assertGreater(app.map_canvas.winfo_height(), 1)
                self.assertTrue(app.context_status_label.winfo_ismapped())
                root.withdraw()

                with patch.object(app, "_start_scan") as start_scan:
                    started = time.monotonic()
                    app._navigate_to(str(deep))
                    elapsed = time.monotonic() - started

                start_scan.assert_not_called()
                self.assertLess(elapsed, 0.5)
                self.assertEqual(
                    app.current_result.root_path,
                    app._normalize_path(str(deep)),
                )
                self.assertIn("内存骨架", app.status_var.get())
                self.assertIn("%", app.free_var.get())

                with patch.object(
                    storage,
                    "get_disk_samples",
                    wraps=storage.get_disk_samples,
                ) as get_samples:
                    app.trend_range_var.set("30 天")
                    app._change_trend_range()
                self.assertEqual(app.trend_hours, 24 * 30)
                self.assertEqual(get_samples.call_args.kwargs["hours"], 24 * 30)

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)
                session = storage.latest_completed_session()
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.end_reason, "quick_close")
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    @patch("disk_monitor.ui.read_disk_sample")
    def test_blind_spot_uses_old_sample_before_pruning(self, read_sample) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            drive = normalize_drive(str(scan_root))
            now = datetime.now()
            old_sample = DiskSample(
                now - timedelta(days=40),
                drive,
                2_000_000_000,
                500_000_000,
                1_500_000_000,
            )
            current_sample = DiskSample(
                now,
                drive,
                2_000_000_000,
                500_000_000 + BLIND_SPOT_THRESHOLD_BYTES + 1,
                1_500_000_000 - BLIND_SPOT_THRESHOLD_BYTES - 1,
            )
            storage.add_disk_sample(old_sample)
            storage.set_setting("close_behavior", "quick")
            read_sample.return_value = current_sample
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
            )
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )

                self.assertIn("⚠", app.blind_spot_var.get())
                samples = storage.get_disk_samples(drive, hours=24 * 50)
                self.assertEqual(len(samples), 1)
                self.assertEqual(samples[0].recorded_at, now.replace(microsecond=0))

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_manual_baseline_switch_can_reach_older_full_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "start.bin").write_bytes(b"a" * 12)
            storage = Storage(base / "monitor.db")
            now = datetime.now()
            sample = DiskSample(now - timedelta(minutes=3), "C:\\", 1000, 400, 600)

            full_start_id = storage.save_scan(scan_path(str(scan_root)), source="baseline")
            (scan_root / "full-change.bin").write_bytes(b"b" * 24)
            full_end_id = storage.save_scan(scan_path(str(scan_root)), source="closing")
            full_session_id = storage.start_session(sample, str(scan_root))
            storage.set_session_start_snapshot(full_session_id, full_start_id)
            storage.finish_session(
                full_session_id,
                DiskSample(now - timedelta(minutes=2), "C:\\", 1000, 424, 576),
                end_snapshot_id=full_end_id,
                end_reason="normal_close",
            )

            quick_start_id = storage.save_scan(scan_path(str(scan_root)), source="baseline")
            quick_session_id = storage.start_session(
                DiskSample(now - timedelta(minutes=1), "C:\\", 1000, 424, 576),
                str(scan_root),
            )
            storage.set_session_start_snapshot(quick_session_id, quick_start_id)
            storage.finish_session(
                quick_session_id,
                DiskSample(now - timedelta(seconds=30), "C:\\", 1000, 424, 576),
                end_reason="quick_close",
            )
            storage.set_setting("close_behavior", "quick")

            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )

                self.assertEqual(
                    app.automatic_baseline_snapshot_id,
                    app.session_start_snapshot_id,
                )
                self.assertEqual(app.latest_full_snapshot_id, full_end_id)
                self.assertEqual(
                    tuple(app.baseline_mode_selector.cget("values")),
                    (
                        BASELINE_MODE_LABELS["startup"],
                        BASELINE_MODE_LABELS["latest_full"],
                    ),
                )

                app.baseline_mode_var.set(BASELINE_MODE_LABELS["latest_full"])
                app._select_baseline_mode()
                self.assertEqual(app.change_context[2], full_end_id)
                self.assertIn("最近完整保存", app.baseline_info_var.get())

                app.baseline_mode_var.set(BASELINE_MODE_LABELS["startup"])
                app._select_baseline_mode()
                self.assertEqual(
                    app.change_context[2], app.session_start_snapshot_id
                )
                self.assertIn("本次启动快照", app.baseline_info_var.get())

                app._restore_default_baseline()
                self.assertEqual(
                    app.change_context[2], app.automatic_baseline_snapshot_id
                )
                app.baseline_mode_var.set(BASELINE_MODE_LABELS["latest_full"])
                app._select_baseline_mode()
                self.assertEqual(app.change_context[2], full_end_id)

                later_sample = DiskSample(
                    app.session_start_sample.recorded_at + timedelta(minutes=5),
                    app.session_start_sample.drive,
                    app.session_start_sample.total_bytes,
                    app.session_start_sample.used_bytes + 223_300_000,
                    app.session_start_sample.free_bytes - 223_300_000,
                )
                app._update_metrics(later_sample)
                self.assertEqual(
                    app.runtime_change_var.get().split(" · ", 1)[0],
                    app.change_var.get(),
                )
                self.assertIn("本次启动快照", app.runtime_change_var.get())
                self.assertIn("重新扫描", app.runtime_change_var.get())

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_treemap_failure_logs_and_restores_scan_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            (scan_root / "first").mkdir(parents=True)
            (scan_root / "second").mkdir()
            (scan_root / "first" / "a.bin").write_bytes(b"a" * 16)
            (scan_root / "second" / "b.bin").write_bytes(b"b" * 8)
            storage = Storage(base / "monitor.db")
            storage.set_setting("close_behavior", "quick")
            log_path = base / "ui.log"
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
                log_path=log_path,
            )
            try:
                with patch.object(
                    app, "_draw_treemap", side_effect=RuntimeError("injected draw failure")
                ):
                    pump_until(
                        root,
                        lambda: app.session_start_snapshot_id is not None
                        and app.active_scan_role is None,
                    )

                self.assertIsNotNone(app.default_change_context)
                self.assertEqual(str(app.scan_button.cget("state")), "normal")
                self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
                self.assertIn("扫描结果界面处理失败", app.status_var.get())
                log_text = log_path.read_text(encoding="utf-8")
                self.assertIn(
                    "ui_operation_failed context=扫描结果界面处理", log_text
                )
                self.assertIn("injected draw failure", log_text)
                self.assertIn("scan_ui_finished role=baseline", log_text)

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_next_start_uses_previous_full_close_and_history_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "old.bin").write_bytes(b"a" * 12)
            storage = Storage(base / "monitor.db")

            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            start_result = scan_path(str(scan_root))
            start_id = storage.save_scan(start_result, source="baseline")
            (scan_root / "new.bin").write_bytes(b"b" * 24)
            end_result = scan_path(str(scan_root))
            end_id = storage.save_scan(end_result, source="closing")
            other_root = base / "other-root"
            other_root.mkdir()
            (other_root / "other.bin").write_bytes(b"c" * 8)
            other_id = storage.save_scan(
                scan_path(str(other_root)), source="manual"
            )
            session_id = storage.start_session(sample, str(scan_root))
            storage.set_session_start_snapshot(session_id, start_id)
            storage.finish_session(
                session_id,
                sample,
                end_snapshot_id=end_id,
                end_reason="normal_close",
            )
            storage.set_setting("close_behavior", "quick")

            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(
                root,
                storage=storage,
                initial_path=str(scan_root),
            )
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )

                self.assertEqual(app.automatic_baseline_snapshot_id, end_id)
                self.assertEqual(app.default_change_context[2], end_id)
                self.assertIn("上次完整保存", app.baseline_info_var.get())

                app.notebook.select(app.history_tab)
                pump_until(root, lambda: app.history_loaded)
                self.assertGreaterEqual(len(app.history_tree.get_children()), 3)
                app.history_tree.selection_set(
                    f"snapshot-{end_id}", f"snapshot-{other_id}"
                )
                app._update_history_selection()
                self.assertEqual(
                    str(app.compare_history_button.cget("state")), "disabled"
                )
                self.assertIn("路径不同", app.history_status_var.get())
                app.history_tree.selection_set(
                    f"snapshot-{start_id}", f"snapshot-{end_id}"
                )
                app._update_history_selection()
                self.assertEqual(
                    str(app.compare_history_button.cget("state")), "normal"
                )
                app.history_tree.selection_remove(*app.history_tree.selection())
                with patch.object(
                    app.history_tree,
                    "identify_row",
                    side_effect=(f"snapshot-{start_id}", f"snapshot-{end_id}"),
                ):
                    app._toggle_history_selection(SimpleNamespace(y=0, state=0))
                    app._toggle_history_selection(SimpleNamespace(y=1, state=0))
                root.update_idletasks()
                self.assertEqual(
                    set(app.history_tree.selection()),
                    {f"snapshot-{start_id}", f"snapshot-{end_id}"},
                )
                app._compare_selected_snapshots()
                self.assertEqual(app.notebook.select(), str(app.changes_tab))
                self.assertEqual(app.change_context[1:3], (end_id, start_id))

                app._on_close()
                pump_until(root, lambda: app.session_finished)
                wait_for_destroy(root)
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass


if __name__ == "__main__":
    unittest.main()
