from __future__ import annotations

import ctypes
import os


ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "Local\\DiskGrowthMonitor") -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if os.name == "nt" and self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


def show_already_running_message() -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None,
            "C 盘空间增长监控器已经在运行。",
            "无法重复启动",
            0x40,
        )
