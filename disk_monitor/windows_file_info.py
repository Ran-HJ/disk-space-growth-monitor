from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_STANDARD_INFO_CLASS = 1
FILE_ID_INFO_CLASS = 18


class _FileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", _FileId128),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("number_of_links", wintypes.DWORD),
        ("delete_pending", wintypes.BOOLEAN),
        ("directory", wintypes.BOOLEAN),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


@dataclass(frozen=True, slots=True)
class FileSpaceInfo:
    allocated_size_bytes: int | None
    volume_serial_hex: str | None
    file_id: bytes | None
    link_count: int | None
    state: str
    error_code: int | None = None

    @property
    def identity_key(self) -> tuple[str, bytes] | None:
        if self.volume_serial_hex is None or self.file_id is None:
            return None
        return self.volume_serial_hex, self.file_id


def file_information_api_status() -> tuple[str, str]:
    """Report native API availability without opening or scanning a user file."""

    if os.name != "nt":
        return "unavailable", "当前系统不是 Windows"
    try:
        _configure_kernel32()
    except (AttributeError, OSError):
        return "unavailable", "Windows 文件信息 API 不可用"
    return "ok", "支持分配大小与稳定文件 ID API"


def _extended_path(path: str) -> str:
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _configure_kernel32() -> object:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _fallback_file_id(
    kernel32: object, handle: int
) -> tuple[str, bytes] | None:
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(
        handle, ctypes.byref(information)
    ):
        return None
    file_index = (
        int(information.file_index_high) << 32
    ) | int(information.file_index_low)
    return (
        f"{int(information.volume_serial_number):016x}",
        file_index.to_bytes(16, byteorder="big"),
    )


def read_file_space_info(
    path: str,
    *,
    expected_size_bytes: int | None = None,
) -> FileSpaceInfo:
    """Read allocation and stable identity metadata without reading file content."""

    if os.name != "nt":
        return FileSpaceInfo(None, None, None, None, "not_windows")

    try:
        kernel32 = _configure_kernel32()
    except (AttributeError, OSError):
        return FileSpaceInfo(None, None, None, None, "unsupported")

    handle = kernel32.CreateFileW(
        _extended_path(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return FileSpaceInfo(
            None,
            None,
            None,
            None,
            "inaccessible",
            ctypes.get_last_error(),
        )

    try:
        standard = _FileStandardInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_STANDARD_INFO_CLASS,
            ctypes.byref(standard),
            ctypes.sizeof(standard),
        ):
            return FileSpaceInfo(
                None,
                None,
                None,
                None,
                "unsupported",
                ctypes.get_last_error(),
            )

        file_id_info = _FileIdInfo()
        identity: tuple[str, bytes] | None = None
        if kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_ID_INFO_CLASS,
            ctypes.byref(file_id_info),
            ctypes.sizeof(file_id_info),
        ):
            file_id = bytes(file_id_info.file_id.identifier)
            if any(file_id):
                identity = (
                    f"{int(file_id_info.volume_serial_number):016x}",
                    file_id,
                )
        if identity is None:
            identity = _fallback_file_id(kernel32, handle)
        if identity is None:
            error_code = ctypes.get_last_error()
            return FileSpaceInfo(
                int(standard.allocation_size),
                None,
                None,
                int(standard.number_of_links),
                "partial",
                error_code or None,
            )
        volume_serial_hex, file_id = identity

        state = "exact"
        if (
            expected_size_bytes is not None
            and int(standard.end_of_file) != expected_size_bytes
        ):
            state = "changed_during_scan"
        return FileSpaceInfo(
            allocated_size_bytes=int(standard.allocation_size),
            volume_serial_hex=volume_serial_hex,
            file_id=file_id,
            link_count=int(standard.number_of_links),
            state=state,
        )
    finally:
        kernel32.CloseHandle(handle)
