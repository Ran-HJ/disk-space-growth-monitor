from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ctypes import wintypes


WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_UPDATE = WM_APP + 2
WM_NOTIFY = WM_APP + 3
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_NULL = 0x0000

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

CMD_SHOW_HIDE = 1001
CMD_FULL_MODE = 1002
CMD_LOW_MODE = 1003
CMD_RESCAN = 1004
CMD_SETTINGS = 1005
CMD_EXIT_QUICK = 1006
CMD_EXIT_FULL = 1007

CommandCallback = Callable[[str], None]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


@dataclass(frozen=True)
class TrayState:
    mode_label: str
    automation_label: str
    window_visible: bool
    scan_busy: bool


class WindowsTrayIcon:
    def __init__(
        self,
        icon_path: str | Path,
        command_callback: CommandCallback,
        *,
        logger: logging.Logger,
    ) -> None:
        self.icon_path = Path(icon_path)
        self.command_callback = command_callback
        self.logger = logger
        self._state = TrayState("全功能模式", "自动：关闭", True, False)
        self._state_lock = threading.Lock()
        self._notifications: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._nid: NOTIFYICONDATAW | None = None
        self._wndproc = WNDPROC(self._window_proc)
        self._class_name = "DiskGrowthMonitorTray-" + uuid.uuid4().hex
        self._taskbar_created = 0

    def start(self, timeout: float = 5.0) -> None:
        if os.name != "nt":
            raise OSError("托盘功能目前仅支持 Windows")
        if not self.icon_path.is_file():
            raise OSError(f"托盘图标不存在：{self.icon_path}")
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="WindowsTrayIcon",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("Windows 托盘启动超时")
        if self._error is not None:
            raise OSError("Windows 托盘启动失败") from self._error

    def stop(self, timeout: float = 5.0) -> None:
        thread = self._thread
        hwnd = self._hwnd
        if thread is not None and thread.is_alive() and hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            thread.join(timeout)
        if thread is not None and thread.is_alive():
            self.logger.warning("tray_thread_stop_timeout")
        self._thread = None

    def update_state(self, state: TrayState) -> None:
        with self._state_lock:
            self._state = state
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_UPDATE, 0, 0)

    def notify(self, title: str, message: str) -> None:
        self._notifications.put((title[:63], message[:255]))
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_NOTIFY, 0, 0)

    def _run(self) -> None:
        try:
            self._initialize_win32()
            self._ready.set()
            message = wintypes.MSG()
            user32 = ctypes.windll.user32
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as error:
            self._error = error
            self.logger.exception("tray_thread_failed")
            self._ready.set()
        finally:
            self._delete_icon()
            self._hwnd = None

    def _initialize_win32(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32
        self._configure_prototypes(user32, kernel32, shell32)
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError()
        hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        self._hwnd = hwnd
        hicon = user32.LoadImageW(
            None,
            str(self.icon_path),
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        if not hicon:
            raise ctypes.WinError()
        self._hicon = hicon
        self._nid = self._new_notify_data()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
            raise ctypes.WinError()
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.logger.info("tray_started")

    @staticmethod
    def _configure_prototypes(user32, kernel32, shell32) -> None:
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        )
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        )
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.GetMessageW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        )
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def _new_notify_data(self) -> NOTIFYICONDATAW:
        assert self._hwnd is not None
        assert self._hicon is not None
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._hicon
        data.szTip = self._tooltip()
        return data

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event == WM_LBUTTONDBLCLK:
                    self._dispatch("show")
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if message == WM_UPDATE:
                self._modify_tooltip()
                return 0
            if message == WM_NOTIFY:
                self._show_pending_notifications()
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._nid = self._new_notify_data()
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_ADD, ctypes.byref(self._nid)
                )
                return 0
            if message == WM_CLOSE:
                ctypes.windll.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception:
            self.logger.exception("tray_window_message_failed message=%s", message)
        return ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError()
        with self._state_lock:
            state = self._state
        try:
            self._append(menu, CMD_SHOW_HIDE, "隐藏主窗口" if state.window_visible else "显示主窗口")
            self._append(menu, 0, f"当前：{state.mode_label}", enabled=False)
            self._append(menu, 0, state.automation_label, enabled=False)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._append(menu, CMD_FULL_MODE, "切换全功能模式", enabled=state.mode_label != "全功能模式")
            self._append(menu, CMD_LOW_MODE, "切换低内存模式", enabled=state.mode_label != "低内存模式")
            self._append(menu, CMD_RESCAN, "重新扫描当前目录", enabled=state.mode_label == "全功能模式" and not state.scan_busy)
            self._append(menu, CMD_SETTINGS, "设置")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._append(menu, CMD_EXIT_QUICK, "快速退出")
            self._append(menu, CMD_EXIT_FULL, "完整保存并退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            command_id = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            if command_id:
                command = {
                    CMD_SHOW_HIDE: "hide" if state.window_visible else "show",
                    CMD_FULL_MODE: "mode_full",
                    CMD_LOW_MODE: "mode_low",
                    CMD_RESCAN: "rescan",
                    CMD_SETTINGS: "settings",
                    CMD_EXIT_QUICK: "exit_quick",
                    CMD_EXIT_FULL: "exit_full",
                }.get(command_id)
                if command:
                    self._dispatch(command)
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    @staticmethod
    def _append(menu, command_id: int, label: str, *, enabled: bool = True) -> None:
        flags = MF_STRING if enabled else MF_STRING | MF_GRAYED
        ctypes.windll.user32.AppendMenuW(menu, flags, command_id, label)

    def _dispatch(self, command: str) -> None:
        try:
            self.command_callback(command)
        except Exception:
            self.logger.exception("tray_command_callback_failed command=%s", command)

    def _tooltip(self) -> str:
        with self._state_lock:
            state = self._state
        return f"C 盘空间增长监控器 · {state.mode_label} · {state.automation_label}"[:127]

    def _modify_tooltip(self) -> None:
        if self._nid is None:
            return
        self._nid.uFlags = NIF_TIP
        self._nid.szTip = self._tooltip()
        ctypes.windll.shell32.Shell_NotifyIconW(
            NIM_MODIFY, ctypes.byref(self._nid)
        )

    def _show_pending_notifications(self) -> None:
        if self._nid is None:
            return
        while True:
            try:
                title, message = self._notifications.get_nowait()
            except queue.Empty:
                return
            self._nid.uFlags = NIF_INFO
            self._nid.szInfoTitle = title
            self._nid.szInfo = message
            self._nid.dwInfoFlags = NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_MODIFY, ctypes.byref(self._nid)
            )

    def _delete_icon(self) -> None:
        if self._nid is not None:
            ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_DELETE, ctypes.byref(self._nid)
            )
            self._nid = None
        if self._hicon:
            ctypes.windll.user32.DestroyIcon(self._hicon)
            self._hicon = None
        self.logger.info("tray_stopped")
