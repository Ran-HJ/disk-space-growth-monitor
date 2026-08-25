from __future__ import annotations

import logging
import os
import unittest
from pathlib import Path

from disk_monitor.tray import TrayState, WindowsTrayIcon


@unittest.skipUnless(os.name == "nt", "Windows 托盘仅在 Windows 运行")
class TrayTests(unittest.TestCase):
    def test_native_tray_starts_updates_dispatches_and_stops(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        commands: list[str] = []
        logger = logging.getLogger(f"test.tray.{id(self)}")
        logger.addHandler(logging.NullHandler())
        tray = WindowsTrayIcon(
            project_root / "assets" / "app.ico",
            commands.append,
            logger=logger,
        )
        tray.start()
        try:
            self.assertIsNotNone(tray._hwnd)
            self.assertTrue(tray._thread and tray._thread.is_alive())
            tray.update_state(
                TrayState("低内存模式", "自动：游戏触发", False, False)
            )
            tray._dispatch("show")
            self.assertEqual(commands, ["show"])
        finally:
            tray.stop()
        self.assertFalse(tray._thread)


if __name__ == "__main__":
    unittest.main()
