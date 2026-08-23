from __future__ import annotations

import os
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DiskGrowthMonitor"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    run_script = Path(__file__).resolve().parent.parent / "run.py"
    return f'"{pythonw}" "{run_script}"'


def is_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key,
                VALUE_NAME,
                0,
                winreg.REG_SZ,
                launch_command(),
            )
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass

