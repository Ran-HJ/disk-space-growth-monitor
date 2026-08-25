from __future__ import annotations

import ctypes
import ntpath
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


DEFAULT_PROCESS_NAMES = ("r5apex.exe", "cs2.exe")


class SettingsStore(Protocol):
    def get_setting(self, key: str, default: str = "") -> str: ...

    def set_setting(self, key: str, value: str) -> None: ...


def normalize_process_names(value: str | tuple[str, ...]) -> tuple[str, ...]:
    raw_names = (
        re.split(r"[;,\r\n]+", value)
        if isinstance(value, str)
        else list(value)
    )
    normalized: list[str] = []
    for raw_name in raw_names:
        name = ntpath.basename(str(raw_name).strip().strip('"')).lower()
        if not name:
            continue
        if not name.endswith(".exe"):
            name += ".exe"
        if name not in normalized:
            normalized.append(name)
    return tuple(normalized)


@dataclass(frozen=True)
class AutoModeConfig:
    enabled: bool = False
    process_names: tuple[str, ...] = DEFAULT_PROCESS_NAMES
    memory_pressure_enabled: bool = True
    high_percent: int = 85
    low_percent: int = 75
    resume_rescan: str = "later"
    enter_samples: int = 2
    exit_samples: int = 6
    poll_interval_ms: int = 5_000

    def validate(self) -> AutoModeConfig:
        if self.enabled and not self.process_names and not self.memory_pressure_enabled:
            raise ValueError("启用自动模式时至少需要进程检测或内存压力检测")
        if not 50 <= self.high_percent <= 99:
            raise ValueError("内存高阈值必须在 50% 到 99% 之间")
        if not 20 <= self.low_percent <= self.high_percent - 5:
            raise ValueError("内存恢复阈值必须至少比高阈值低 5 个百分点")
        if self.resume_rescan not in {"now", "later"}:
            raise ValueError("恢复补扫策略必须是 now 或 later")
        if self.enter_samples < 1 or self.exit_samples < 1:
            raise ValueError("自动切换稳定样本数必须是正整数")
        if self.poll_interval_ms < 1_000:
            raise ValueError("自动检测间隔不能小于 1 秒")
        return self

    @classmethod
    def load(cls, storage: SettingsStore) -> AutoModeConfig:
        def integer_setting(key: str, default: int) -> int:
            raw_value = storage.get_setting(key, str(default))
            try:
                return int(raw_value)
            except ValueError:
                return default

        config = cls(
            enabled=storage.get_setting("auto_mode_enabled", "0") == "1",
            process_names=normalize_process_names(
                storage.get_setting(
                    "auto_process_names", ";".join(DEFAULT_PROCESS_NAMES)
                )
            ),
            memory_pressure_enabled=(
                storage.get_setting("auto_memory_pressure_enabled", "1") == "1"
            ),
            high_percent=integer_setting("auto_memory_high_percent", 85),
            low_percent=integer_setting("auto_memory_low_percent", 75),
            resume_rescan=storage.get_setting("auto_resume_rescan", "later"),
        )
        try:
            return config.validate()
        except ValueError:
            return cls()

    def save(self, storage: SettingsStore) -> None:
        self.validate()
        storage.set_setting("auto_mode_enabled", "1" if self.enabled else "0")
        storage.set_setting("auto_process_names", ";".join(self.process_names))
        storage.set_setting(
            "auto_memory_pressure_enabled",
            "1" if self.memory_pressure_enabled else "0",
        )
        storage.set_setting("auto_memory_high_percent", str(self.high_percent))
        storage.set_setting("auto_memory_low_percent", str(self.low_percent))
        storage.set_setting("auto_resume_rescan", self.resume_rescan)

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "process_names": list(self.process_names),
            "memory_pressure_enabled": self.memory_pressure_enabled,
            "high_percent": self.high_percent,
            "low_percent": self.low_percent,
            "resume_rescan": self.resume_rescan,
            "poll_interval_ms": self.poll_interval_ms,
            "enter_samples": self.enter_samples,
            "exit_samples": self.exit_samples,
        }


@dataclass(frozen=True)
class AutoObservation:
    memory_percent: int
    matched_processes: tuple[str, ...] = ()
    observed_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_percent": self.memory_percent,
            "matched_processes": list(self.matched_processes),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class AutoDecision:
    action: str | None
    status: str
    reason: str
    triggers: tuple[str, ...]
    enter_count: int
    exit_count: int


class AutoModePolicy:
    def __init__(self, config: AutoModeConfig) -> None:
        self.config = config.validate()
        self.pressure_latched = False
        self.owns_low_mode = False
        self.manual_hold = False
        self.enter_count = 0
        self.exit_count = 0
        self.current_triggers: tuple[str, ...] = ()

    def update_config(self, config: AutoModeConfig) -> None:
        was_enabled = self.config.enabled
        self.config = config.validate()
        self.enter_count = 0
        self.exit_count = 0
        self.pressure_latched = False
        if was_enabled and not config.enabled:
            self.owns_low_mode = False
            self.manual_hold = False

    def note_manual_mode_change(self) -> None:
        self.owns_low_mode = False
        self.manual_hold = bool(self.current_triggers)
        self.enter_count = 0
        self.exit_count = 0

    def mark_auto_entered(self) -> None:
        self.owns_low_mode = True
        self.enter_count = 0
        self.exit_count = 0

    def mark_auto_left(self) -> None:
        self.owns_low_mode = False
        self.enter_count = 0
        self.exit_count = 0

    def evaluate(
        self,
        observation: AutoObservation,
        *,
        run_mode: str,
        scan_busy: bool,
    ) -> AutoDecision:
        config = self.config
        if config.memory_pressure_enabled:
            if observation.memory_percent >= config.high_percent:
                self.pressure_latched = True
            elif observation.memory_percent <= config.low_percent:
                self.pressure_latched = False
        else:
            self.pressure_latched = False

        triggers: list[str] = []
        if observation.matched_processes:
            triggers.append("process")
        if self.pressure_latched:
            triggers.append("memory_pressure")
        self.current_triggers = tuple(triggers)

        if not config.enabled:
            self.enter_count = 0
            self.exit_count = 0
            return self._decision(None, "disabled", "自动模式未启用")

        if self.manual_hold:
            self.enter_count = 0
            self.exit_count = 0
            if triggers:
                return self._decision(
                    None,
                    "manual_override",
                    "已按人工选择暂停自动控制，等待触发条件消失",
                )
            self.manual_hold = False
            return self._decision(None, "monitoring", "人工让权已结束，继续监控")

        if run_mode == "full":
            if self.owns_low_mode:
                self.owns_low_mode = False
            self.exit_count = 0
            if not triggers:
                self.enter_count = 0
                return self._decision(None, "monitoring", "未检测到自动触发条件")
            self.enter_count += 1
            reason = self._trigger_reason(observation)
            if self.enter_count < config.enter_samples:
                return self._decision(None, "detecting", reason)
            if scan_busy:
                return self._decision(None, "waiting_for_scan", reason)
            return self._decision("enter_low", "switching", reason)

        self.enter_count = 0
        if not self.owns_low_mode:
            self.exit_count = 0
            return self._decision(None, "manual_low", "当前低内存模式由用户控制")
        if triggers:
            self.exit_count = 0
            return self._decision(
                None, "auto_low", self._trigger_reason(observation)
            )
        self.exit_count += 1
        if self.exit_count < config.exit_samples:
            return self._decision(
                None,
                "recovering",
                "触发条件已消失，等待稳定后恢复全功能模式",
            )
        return self._decision(
            "leave_low",
            "switching",
            "触发条件已稳定消失",
        )

    def _decision(
        self, action: str | None, status: str, reason: str
    ) -> AutoDecision:
        return AutoDecision(
            action=action,
            status=status,
            reason=reason,
            triggers=self.current_triggers,
            enter_count=self.enter_count,
            exit_count=self.exit_count,
        )

    def _trigger_reason(self, observation: AutoObservation) -> str:
        parts: list[str] = []
        if observation.matched_processes:
            parts.append("检测到进程：" + ", ".join(observation.matched_processes))
        if self.pressure_latched:
            parts.append(f"系统内存占用 {observation.memory_percent}%")
        return "；".join(parts) or "检测到自动触发条件"


class WindowsSystemProbe:
    def observe(self, watched_processes: tuple[str, ...]) -> AutoObservation:
        if os.name != "nt":
            raise OSError("自动模式系统检测目前仅支持 Windows")
        running = _running_process_names()
        matched = tuple(name for name in watched_processes if name in running)
        return AutoObservation(
            memory_percent=_memory_load_percent(),
            matched_processes=matched,
            observed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )


def _memory_load_percent() -> int:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return int(status.dwMemoryLoad)


def _running_process_names() -> set[str]:
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    names: set[str] = set()
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                names.add(entry.szExeFile.lower())
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return names
