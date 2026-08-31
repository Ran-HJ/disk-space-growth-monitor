from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Protocol


DEFAULT_DPI = 96
POINTS_PER_INCH = 72
PER_MONITOR_AWARE_V2 = -4
ERROR_ACCESS_DENIED = 5
MONITOR_DEFAULTTONEAREST = 2


class TkWindow(Protocol):
    tk: object

    def winfo_id(self) -> int: ...

    def winfo_fpixels(self, value: str) -> float: ...


@dataclass(frozen=True)
class DisplayMetrics:
    dpi: int
    previous_scaling: float
    target_scaling: float
    changed: bool


@dataclass(frozen=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def enable_per_monitor_dpi_awareness() -> str:
    """Enable the sharpest DPI mode available before Tk creates a window."""
    if os.name != "nt":
        return "unsupported"

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
            return "per_monitor_v2"
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return "already_configured"
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        result = int(set_awareness(2))
        if result == 0:
            return "per_monitor"
        if result & 0xFFFFFFFF == 0x80070005:
            return "already_configured"
    except (AttributeError, OSError):
        pass

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_aware = user32.SetProcessDPIAware
        set_aware.argtypes = []
        set_aware.restype = ctypes.c_bool
        if set_aware():
            return "system_aware"
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return "already_configured"
    except (AttributeError, OSError):
        pass
    return "unavailable"


def get_window_dpi(window: TkWindow) -> int:
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = [ctypes.c_void_p]
            get_dpi.restype = ctypes.c_uint
            dpi = int(get_dpi(ctypes.c_void_p(window.winfo_id())))
            if dpi > 0:
                return dpi
        except (AttributeError, OSError):
            pass
    try:
        return max(1, round(float(window.winfo_fpixels("1i"))))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_DPI


def sync_tk_scaling(window: TkWindow) -> DisplayMetrics:
    dpi = get_window_dpi(window)
    target = dpi / POINTS_PER_INCH
    previous = float(window.tk.call("tk", "scaling"))
    changed = abs(previous - target) >= 0.01
    if changed:
        window.tk.call("tk", "scaling", target)
    return DisplayMetrics(dpi, previous, target, changed)


def clamp_window_position(
    width: int,
    height: int,
    desired_x: int,
    desired_y: int,
    work_area: WorkArea,
    *,
    margin: int = 12,
) -> tuple[int, int]:
    maximum_x = max(work_area.left + margin, work_area.right - width - margin)
    maximum_y = max(work_area.top + margin, work_area.bottom - height - margin)
    x = min(max(desired_x, work_area.left + margin), maximum_x)
    y = min(max(desired_y, work_area.top + margin), maximum_y)
    return x, y


def cursor_work_area() -> tuple[int, int, WorkArea] | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
        user32.GetCursorPos.restype = ctypes.c_bool
        user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_ulong]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_MonitorInfo),
        ]
        user32.GetMonitorInfoW.restype = ctypes.c_bool
        point = _Point()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        monitor = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return point.x, point.y, WorkArea(
            work.left,
            work.top,
            work.right,
            work.bottom,
        )
    except (AttributeError, OSError):
        return None


def position_near_cursor(window: object) -> tuple[int, int]:
    window.update_idletasks()
    width = int(window.winfo_reqwidth())
    height = int(window.winfo_reqheight())
    cursor = cursor_work_area()
    if cursor is None:
        area = WorkArea(
            0,
            0,
            int(window.winfo_screenwidth()),
            int(window.winfo_screenheight()),
        )
        desired_x = (area.right - width) // 2
        desired_y = (area.bottom - height) // 2
    else:
        cursor_x, cursor_y, area = cursor
        desired_x = cursor_x - width + 24
        desired_y = cursor_y - height + 24
    x, y = clamp_window_position(width, height, desired_x, desired_y, area)
    window.geometry(f"{x:+d}{y:+d}")
    return x, y
