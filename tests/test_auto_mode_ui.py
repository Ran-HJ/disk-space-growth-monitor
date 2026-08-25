from __future__ import annotations

import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

from disk_monitor.automation import AutoObservation
from disk_monitor.control_transport import ControlClient
from disk_monitor.storage import Storage
from disk_monitor.ui import DiskMonitorApp


def pump_until(root: tk.Tk, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待界面操作完成超时")


def control_request(
    root: tk.Tk,
    client: ControlClient,
    command: str,
    args: dict | None = None,
) -> dict:
    outcome: dict[str, dict] = {}

    def worker() -> None:
        outcome["response"] = client.request(command, args)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    pump_until(root, lambda: not thread.is_alive())
    thread.join(timeout=1)
    return outcome["response"]


class AutoModeUiTests(unittest.TestCase):
    def test_tray_commands_are_applied_by_the_gui_message_loop(self) -> None:
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
                app.messages.put(("tray_command", "mode_low"))
                pump_until(root, lambda: app.run_mode == "low_memory")
                self.assertFalse(app.auto_mode_policy.owns_low_mode)

                app.messages.put(("tray_command", "mode_full"))
                pump_until(root, lambda: app.run_mode == "full")
                self.assertIsNone(app.active_scan_role)
                self.assertIn("稍后手动扫描", app.status_var.get())
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_agent_configures_auto_mode_and_policy_switches_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 32)
            storage = Storage(base / "monitor.db")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            client = ControlClient(base)
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )
                configured = control_request(
                    root,
                    client,
                    "automation.configure",
                    {
                        "enabled": True,
                        "process_names": ["game.exe"],
                        "memory_pressure_enabled": False,
                        "high_percent": 90,
                        "low_percent": 70,
                        "resume_rescan": "later",
                    },
                )
                self.assertTrue(configured["ok"])
                self.assertTrue(configured["data"]["config"]["enabled"])
                self.assertEqual(storage.get_setting("auto_mode_enabled"), "1")

                game = AutoObservation(40, ("game.exe",), "game-start")
                first = app._run_automation_check(game)
                self.assertEqual(first.status, "detecting")
                second = app._run_automation_check(game)
                self.assertEqual(second.action, "enter_low")
                self.assertEqual(app.run_mode, "low_memory")
                self.assertTrue(app.auto_mode_policy.owns_low_mode)

                clear = AutoObservation(40, (), "game-end")
                for _ in range(app.auto_mode_config.exit_samples):
                    app._run_automation_check(clear)
                self.assertEqual(app.run_mode, "full")
                self.assertFalse(app.auto_mode_policy.owns_low_mode)
                self.assertIsNone(app.active_scan_role)
                self.assertIn("稍后手动补扫", app.status_var.get())

                status = control_request(root, client, "automation.status")
                self.assertTrue(status["ok"])
                self.assertEqual(status["data"]["status"], "monitoring")
                self.assertEqual(
                    status["data"]["observation"]["observed_at"], "game-end"
                )
            finally:
                app.session_finished = True
                app._destroy_root()

    def test_agent_manual_low_mode_keeps_control_after_trigger_clears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            storage = Storage(base / "monitor.db")
            root = tk.Tk()
            root.withdraw()
            app = DiskMonitorApp(root, storage=storage, initial_path=str(scan_root))
            client = ControlClient(base)
            try:
                pump_until(
                    root,
                    lambda: app.session_start_snapshot_id is not None
                    and app.active_scan_role is None,
                )
                control_request(
                    root,
                    client,
                    "automation.configure",
                    {
                        "enabled": True,
                        "process_names": ["game.exe"],
                        "memory_pressure_enabled": False,
                    },
                )
                manual = control_request(
                    root,
                    client,
                    "mode.set",
                    {"mode": "low_memory", "rescan": None},
                )
                self.assertTrue(manual["ok"])
                self.assertFalse(app.auto_mode_policy.owns_low_mode)

                for _ in range(app.auto_mode_config.exit_samples + 2):
                    app._run_automation_check(
                        AutoObservation(40, (), "clear")
                    )
                self.assertEqual(app.run_mode, "low_memory")
                self.assertEqual(app.automation_status_code, "manual_low")

                invalid = control_request(
                    root,
                    client,
                    "automation.configure",
                    {"high_percent": 70, "low_percent": 69},
                )
                self.assertFalse(invalid["ok"])
                self.assertEqual(invalid["code"], "invalid_args")
                self.assertEqual(app.auto_mode_config.high_percent, 85)
            finally:
                app.session_finished = True
                app._destroy_root()


if __name__ == "__main__":
    unittest.main()
