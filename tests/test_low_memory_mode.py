from __future__ import annotations

import tempfile
import time
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from disk_monitor.models import DiskSample
from disk_monitor.scanner import scan_path
from disk_monitor.storage import Storage
from disk_monitor.ui import (
    RUN_MODE_FULL,
    RUN_MODE_LOW_MEMORY,
    DiskMonitorApp,
)


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


def later_sample(sample: DiskSample, change_bytes: int) -> DiskSample:
    return DiskSample(
        sample.recorded_at + timedelta(minutes=5),
        sample.drive,
        sample.total_bytes,
        sample.used_bytes + change_bytes,
        sample.free_bytes - change_bytes,
    )


class LowMemoryModeTests(unittest.TestCase):
    def test_process_exit_failure_is_logged_and_left_for_recovery(self) -> None:
        app = object.__new__(DiskMonitorApp)
        app.session_id = 1
        app.session_finished = False
        app.session_root_path = "C:\\"
        app.storage = Mock()
        app.logger = Mock()

        with patch(
            "disk_monitor.ui.read_disk_sample",
            side_effect=RuntimeError("injected process-exit failure"),
        ):
            app._finalize_on_process_exit()

        app.logger.exception.assert_called_once_with("process_exit_finalize_failed")
        self.assertFalse(app.session_finished)

    def test_mode_switch_waits_for_first_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                with patch("disk_monitor.ui.messagebox.showinfo") as showinfo:
                    self.assertFalse(
                        app._request_run_mode(RUN_MODE_LOW_MEMORY)
                    )
                showinfo.assert_called_once()
                self.assertEqual(app.run_mode, RUN_MODE_FULL)
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_cold_start_low_memory_persists_and_builds_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
            storage.set_setting("run_mode", RUN_MODE_LOW_MEMORY)
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(root, lambda: app.session_id is not None)

                self.assertEqual(app.run_mode, RUN_MODE_LOW_MEMORY)
                self.assertEqual(
                    storage.get_setting("run_mode"), RUN_MODE_LOW_MEMORY
                )
                self.assertIsNone(app.session_start_snapshot_id)
                self.assertIsNone(app.active_scan_role)
                self.assertFalse(app.baseline_pending)
                self.assertEqual(str(app.scan_button.cget("state")), "disabled")
                self.assertIn("低内存模式：未扫描", app.detail_var.get())
                self.assertIn("模式：低内存", app.context_status_var.get())
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_full_to_low_releases_scan_state_and_uses_reference_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
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
                reference_id = app.automatic_current_snapshot_id
                self.assertIsNotNone(reference_id)
                self.assertIsNotNone(app.navigation_skeleton)
                self.assertTrue(app.nav_cache)

                self.assertTrue(app._request_run_mode(RUN_MODE_LOW_MEMORY))

                self.assertEqual(app.run_mode, RUN_MODE_LOW_MEMORY)
                self.assertEqual(app.low_memory_reference_snapshot_id, reference_id)
                self.assertIsNone(app.current_result)
                self.assertIsNone(app.navigation_skeleton)
                self.assertEqual(app.nav_cache, {})
                self.assertEqual(app.rectangle_items, [])
                self.assertIsNone(app.change_context)
                self.assertIn("无实时快照对比", app.growth_subtitle_var.get())

                session_start_id = app.session_start_snapshot_id
                other_root = base / "other-root"
                other_root.mkdir()
                other_id = storage.save_scan(scan_path(str(other_root)), source="manual")
                app.automatic_current_snapshot_id = other_id
                self.assertEqual(
                    app._select_low_memory_reference(), session_start_id
                )
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_switch_to_low_is_rejected_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )
                original_thread = app.scan_thread
                app.scan_thread = SimpleNamespace(is_alive=lambda: False)
                app.active_scan_role = "manual"
                with patch("disk_monitor.ui.messagebox.showinfo") as showinfo:
                    dialog_parent = object()
                    self.assertFalse(
                        app._request_run_mode(
                            RUN_MODE_LOW_MEMORY,
                            parent=dialog_parent,
                        )
                    )
                    self.assertIs(
                        showinfo.call_args.kwargs["parent"], dialog_parent
                    )
                showinfo.assert_called_once()
                self.assertEqual(app.run_mode, RUN_MODE_FULL)
                app.scan_thread = original_thread
                app.active_scan_role = None
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_low_memory_close_skips_dialog_and_address_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            storage.set_setting("run_mode", RUN_MODE_LOW_MEMORY)
            storage.set_setting("close_behavior", "ask")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(root, lambda: app.session_id is not None)
                with patch("disk_monitor.ui.CloseChoiceDialog") as close_dialog:
                    app._on_close()
                close_dialog.assert_not_called()
                wait_for_destroy(root)

                session = storage.latest_completed_session()
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.end_reason, "low_memory_close")
                self.assertIsNone(session.start_snapshot_id)
                self.assertIsNone(session.end_snapshot_id)
                self.assertEqual(storage.list_snapshots(limit=10), [])
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_low_memory_close_waits_for_residual_scan_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            storage.set_setting("run_mode", RUN_MODE_LOW_MEMORY)
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(root, lambda: app.session_id is not None)
                app.cancel_event.clear()
                app.active_scan_role = "manual"
                app.scan_thread = SimpleNamespace(
                    is_alive=lambda: not app.cancel_event.is_set()
                )

                app._on_close()

                self.assertTrue(app.cancel_event.is_set())
                self.assertTrue(root.winfo_exists())
                app.messages.put(("scan_cancelled", "manual"))
                wait_for_destroy(root)
                session = storage.latest_completed_session()
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.end_reason, "low_memory_close")
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_cold_low_to_full_builds_new_baseline_and_keeps_disk_scopes_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
            storage.set_setting("run_mode", RUN_MODE_LOW_MEMORY)
            storage.set_setting("close_behavior", "quick")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(root, lambda: app.low_memory_start_sample is not None)
                low_end = later_sample(app.low_memory_start_sample, 120_000_000)
                app._update_metrics(low_end)

                with patch(
                    "disk_monitor.ui.messagebox.showinfo"
                ) as showinfo, patch(
                    "disk_monitor.ui.read_disk_sample", return_value=low_end
                ) as read_sample:
                    self.assertTrue(app._request_run_mode(RUN_MODE_FULL))
                read_sample.assert_called_once()
                self.assertIn("此前只有磁盘口径记录", showinfo.call_args.args[1])
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )

                self.assertEqual(app.run_mode, RUN_MODE_FULL)
                self.assertIn("本次建立新基线", app.growth_subtitle_var.get())
                self.assertIn("+114.4 MB", app.low_memory_change_var.get())
                self.assertNotEqual(
                    app.low_memory_change_var.get(), app.blind_spot_var.get()
                )
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_low_to_full_yes_uses_reference_and_no_or_cancel_do_not_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
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
                reference_id = app.automatic_current_snapshot_id
                self.assertTrue(app._request_run_mode(RUN_MODE_LOW_MEMORY))

                with patch(
                    "disk_monitor.ui.messagebox.askyesnocancel", return_value=None
                ):
                    self.assertFalse(app._request_run_mode(RUN_MODE_FULL))
                self.assertEqual(app.run_mode, RUN_MODE_LOW_MEMORY)
                self.assertEqual(
                    storage.get_setting("run_mode"), RUN_MODE_LOW_MEMORY
                )

                with patch(
                    "disk_monitor.ui.messagebox.askyesnocancel", return_value=False
                ):
                    self.assertTrue(app._request_run_mode(RUN_MODE_FULL))
                self.assertEqual(app.run_mode, RUN_MODE_FULL)
                self.assertEqual(storage.get_setting("run_mode"), RUN_MODE_FULL)
                self.assertIsNone(app.current_result)
                self.assertIn("数据尚未重建", app.treemap_placeholder_text)
                self.assertEqual(str(app.scan_button.cget("state")), "normal")

                self.assertTrue(app._request_run_mode(RUN_MODE_LOW_MEMORY))
                with patch(
                    "disk_monitor.ui.messagebox.askyesnocancel", return_value=True
                ):
                    self.assertTrue(app._request_run_mode(RUN_MODE_FULL))
                pump_until(root, lambda: app.active_scan_role is None)
                self.assertEqual(app.change_context[2], reference_id)
                self.assertIn("自参考快照（", app.growth_subtitle_var.get())
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_history_comparison_in_low_memory_has_scope_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "old.bin").write_bytes(b"x" * 16)
            storage = Storage(base / "monitor.db")
            old_id = storage.save_scan(scan_path(str(scan_root)), source="manual")
            (scan_root / "new.bin").write_bytes(b"y" * 24)
            new_id = storage.save_scan(scan_path(str(scan_root)), source="manual")
            storage.set_setting("run_mode", RUN_MODE_LOW_MEMORY)
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            try:
                pump_until(root, lambda: app.session_id is not None)
                app._load_snapshot_history(reset=True)
                app.history_tree.selection_set(
                    f"snapshot-{old_id}", f"snapshot-{new_id}"
                )
                app._compare_selected_snapshots()

                self.assertEqual(app.change_context[1:3], (new_id, old_id))
                self.assertEqual(
                    app.growth_subtitle_var.get(),
                    "历史快照对比，不代表本次低内存期间变化",
                )
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_full_resume_without_reference_builds_new_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 16)
            storage = Storage(base / "monitor.db")
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
                app.automatic_current_snapshot_id = None
                app.session_start_snapshot_id = None
                self.assertTrue(app._request_run_mode(RUN_MODE_LOW_MEMORY))
                self.assertIsNone(app.low_memory_reference_snapshot_id)

                with patch(
                    "disk_monitor.ui.messagebox.askyesnocancel", return_value=True
                ):
                    self.assertTrue(app._request_run_mode(RUN_MODE_FULL))
                pump_until(root, lambda: app.active_scan_role is None)

                self.assertIn(
                    "此前无同路径快照，本次建立新基线",
                    app.growth_subtitle_var.get(),
                )
            finally:
                app.session_finished = True
                app._destroy_root()


if __name__ == "__main__":
    unittest.main()
