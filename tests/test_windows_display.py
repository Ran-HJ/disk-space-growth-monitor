from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from disk_monitor.ui import DiskMonitorApp
from disk_monitor.windows_display import (
    WorkArea,
    clamp_window_position,
    enable_per_monitor_dpi_awareness,
    position_near_cursor,
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


class _PositionWindow:
    def __init__(self) -> None:
        self.geometry_value = ""

    def update_idletasks(self) -> None:
        pass

    def winfo_reqwidth(self) -> int:
        return 520

    def winfo_reqheight(self) -> int:
        return 640

    def geometry(self, value: str) -> None:
        self.geometry_value = value


class _MainWindow:
    def __init__(self) -> None:
        self.geometry_value = ""
        self.minimum_size: tuple[int, int] | None = None

    def title(self, _value: str) -> None:
        pass

    def geometry(self, value: str) -> None:
        self.geometry_value = value

    def minsize(self, width: int, height: int) -> None:
        self.minimum_size = (width, height)

    def configure(self, **_kwargs) -> None:
        pass

    def iconbitmap(self, **_kwargs) -> None:
        pass

    def bind(self, *_args, **_kwargs) -> None:
        pass


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

    @patch(
        "disk_monitor.windows_display.cursor_work_area",
        return_value=(-1000, 500, WorkArea(-1920, 0, 0, 1040)),
    )
    def test_positions_popup_with_valid_signed_negative_geometry(
        self, _cursor_work_area
    ) -> None:
        window = _PositionWindow()

        position = position_near_cursor(window)

        self.assertEqual(position, (-1496, 12))
        self.assertEqual(window.geometry_value, "-1496+12")

    @patch(
        "disk_monitor.ui.cursor_work_area",
        return_value=(-1500, -500, WorkArea(-1920, -1000, -1024, -300)),
    )
    def test_main_window_fits_small_negative_coordinate_work_area(
        self, _cursor_work_area
    ) -> None:
        root = _MainWindow()
        app = DiskMonitorApp.__new__(DiskMonitorApp)
        app.root = root
        app.ui_scale = 1.0

        app._configure_window()

        self.assertEqual(root.geometry_value, "872x676-1908-988")
        self.assertEqual(root.minimum_size, (872, 676))


if __name__ == "__main__":
    unittest.main()
