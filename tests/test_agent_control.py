from __future__ import annotations

import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from disk_monitor.control_transport import ControlClient
from disk_monitor.models import ScanProgress
from disk_monitor.scanner import ScanCancelled
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
    *,
    timeout: float = 8.0,
) -> dict:
    outcome: dict[str, dict] = {}

    def worker() -> None:
        outcome["response"] = client.request(command, args)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    pump_until(root, lambda: not thread.is_alive(), timeout=timeout)
    thread.join(timeout=1)
    return outcome["response"]


class AgentControlTests(unittest.TestCase):
    def test_mode_scan_navigation_snapshot_queries_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            child = scan_root / "child"
            child.mkdir(parents=True)
            (scan_root / "root.bin").write_bytes(b"r" * 32)
            (child / "child.bin").write_bytes(b"c" * 64)
            storage = Storage(base / "monitor.db")
            storage.set_setting("close_behavior", "quick")
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

                status = control_request(root, client, "app.status")
                self.assertTrue(status["ok"])
                self.assertTrue(status["data"]["running"])
                self.assertEqual(status["data"]["path"], str(scan_root).lower())

                low = control_request(
                    root,
                    client,
                    "mode.set",
                    {"mode": "low_memory", "rescan": None},
                )
                self.assertTrue(low["ok"])
                self.assertEqual(app.run_mode, "low_memory")
                rejected = control_request(
                    root, client, "scan.start", {"path": str(scan_root)}
                )
                self.assertFalse(rejected["ok"])
                self.assertEqual(rejected["code"], "low_memory_mode")

                full = control_request(
                    root,
                    client,
                    "mode.set",
                    {"mode": "full", "rescan": "later"},
                )
                self.assertTrue(full["ok"])
                self.assertEqual(app.run_mode, "full")

                started = control_request(
                    root, client, "scan.start", {"path": str(scan_root)}
                )
                self.assertTrue(started["ok"])
                task_id = started["data"]["request_id"]
                pump_until(root, lambda: app.active_scan_role is None)
                task = control_request(
                    root, client, "scan.status", {"request_id": task_id}
                )
                self.assertEqual(task["data"]["state"], "completed")
                self.assertIsNotNone(task["data"]["result"]["snapshot_id"])

                view = control_request(root, client, "view.current")
                self.assertEqual(view["data"]["result"]["root_path"], str(scan_root).lower())
                tree = control_request(root, client, "tree.current", {"limit": 20})
                self.assertTrue(tree["ok"])
                self.assertTrue(tree["data"]["items"])
                opened = control_request(
                    root,
                    client,
                    "view.open",
                    {"path": str(child), "scan_if_missing": False},
                )
                self.assertTrue(opened["ok"])
                self.assertIn(opened["data"]["source"], {"memory_skeleton", "session_cache"})

                saved = control_request(
                    root, client, "snapshot.save", {"note": "agent-e2e"}
                )
                self.assertTrue(saved["ok"])
                saved_task_id = saved["data"]["request_id"]
                pump_until(root, lambda: app.active_scan_role is None)
                saved_task = control_request(
                    root,
                    client,
                    "scan.result",
                    {"request_id": saved_task_id},
                )
                self.assertEqual(saved_task["data"]["state"], "completed")
                snapshots = storage.list_snapshots(limit=20)
                self.assertTrue(any(item.note == "agent-e2e" for item in snapshots))

                growth = control_request(
                    root,
                    client,
                    "growth.current",
                    {"direction": "increase", "limit": 20},
                )
                self.assertTrue(growth["ok"])
                self.assertIn("session_used_change_bytes", growth["data"])

                closed = control_request(
                    root, client, "app.close", {"behavior": "quick"}
                )
                self.assertTrue(closed["ok"])
                pump_until(
                    root,
                    lambda: not (base / "control.endpoint.json").exists(),
                )
                self.assertFalse(list(base.glob("control-*.auth")))
            finally:
                app.session_finished = True
                try:
                    app._destroy_root()
                except tk.TclError:
                    pass

    def test_agent_can_cancel_running_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scan_root = base / "scan-root"
            scan_root.mkdir()
            (scan_root / "data.bin").write_bytes(b"x" * 16)
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

                def slow_scan(path, *, cancel_event, progress_callback):
                    while not cancel_event.wait(0.01):
                        progress_callback(
                            ScanProgress(str(path), 16, 1, 1, 0)
                        )
                    raise ScanCancelled()

                with patch("disk_monitor.ui.scan_path", side_effect=slow_scan):
                    started = control_request(
                        root,
                        client,
                        "scan.start",
                        {"path": str(scan_root)},
                    )
                    self.assertTrue(started["ok"])
                    task_id = started["data"]["request_id"]
                    cancelled = control_request(root, client, "scan.cancel")
                    self.assertTrue(cancelled["ok"])
                    pump_until(root, lambda: app.active_scan_role is None)

                task = control_request(
                    root, client, "scan.status", {"request_id": task_id}
                )
                self.assertEqual(task["data"]["state"], "cancelled")
                result = control_request(
                    root, client, "scan.result", {"request_id": task_id}
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "scan_cancelled")
            finally:
                app.session_finished = True
                app._destroy_root()


if __name__ == "__main__":
    unittest.main()
