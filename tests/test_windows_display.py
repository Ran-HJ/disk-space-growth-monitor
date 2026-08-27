from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from disk_monitor.windows_display import (
    WorkArea,
    clamp_window_position,
    enable_per_monitor_dpi_awareness,
    sync_tk_scaling,
)


class _FakeTk:
    def __init__(self, scaling: float) -> None:
        self.scaling = scaling
        self.calls: list[tuple] = []

    def call(self, *args):
        self.calls.append(args)
        if args == ("tk", "scaling"):
            return self.scaling
        if args[:2] == ("tk", "scaling"):
            self.scaling = float(args[2])
            return ""
        raise AssertionError(args)


class _FakeWindow:
    def __init__(self, scaling: float = 1.333) -> None:
        self.tk = _FakeTk(scaling)

    def winfo_id(self) -> int:
        return 123

    def winfo_fpixels(self, _value: str) -> float:
        return 96.0


class WindowsDisplayTests(unittest.TestCase):
    @patch("disk_monitor.windows_display.get_window_dpi", return_value=192)
    def test_sync_tk_scaling_uses_window_dpi(self, _get_dpi) -> None:
        window = _FakeWindow()

        metrics = sync_tk_scaling(window)

        self.assertEqual(metrics.dpi, 192)
        self.assertAlmostEqual(metrics.target_scaling, 192 / 72)
        self.assertTrue(metrics.changed)
        self.assertAlmostEqual(window.tk.scaling, 192 / 72)

    @patch("disk_monitor.windows_display.get_window_dpi", return_value=192)
    def test_sync_tk_scaling_is_idempotent(self, _get_dpi) -> None:
        window = _FakeWindow(192 / 72)

        metrics = sync_tk_scaling(window)

        self.assertFalse(metrics.changed)
        self.assertEqual(len(window.tk.calls), 1)

    @patch("disk_monitor.windows_display.os.name", "nt")
    @patch("disk_monitor.windows_display.ctypes.WinDLL")
    def test_enables_per_monitor_v2_before_using_fallbacks(self, win_dll) -> None:
        user32 = MagicMock()
        user32.SetProcessDpiAwarenessContext.return_value = True
        win_dll.return_value = user32

        status = enable_per_monitor_dpi_awareness()

        self.assertEqual(status, "per_monitor_v2")
        win_dll.assert_called_once_with("user32", use_last_error=True)

    def test_clamps_popup_inside_negative_coordinate_work_area(self) -> None:
        work_area = WorkArea(-1920, 0, 0, 1040)

        x, y = clamp_window_position(520, 640, -2500, 900, work_area)

        self.assertEqual(x, -1908)
        self.assertEqual(y, 388)


if __name__ == "__main__":
    unittest.main()
