from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from disk_monitor import autostart


def fake_winreg() -> MagicMock:
    module = MagicMock()
    module.HKEY_CURRENT_USER = object()
    module.KEY_SET_VALUE = 2
    module.REG_SZ = 1
    return module


class AutostartTests(unittest.TestCase):
    def test_non_windows_autostart_is_disabled_and_write_is_noop(self) -> None:
        with patch.object(autostart.os, "name", "posix"):
            self.assertFalse(autostart.is_autostart_enabled())
            autostart.set_autostart(True)

    def test_windows_autostart_reads_and_writes_current_user_run_key(self) -> None:
        winreg = fake_winreg()
        read_key = MagicMock()
        write_key = MagicMock()
        winreg.OpenKey.return_value.__enter__.return_value = read_key
        winreg.CreateKey.return_value.__enter__.return_value = write_key

        with patch.object(autostart.os, "name", "nt"), patch.dict(
            sys.modules, {"winreg": winreg}
        ), patch.object(autostart, "launch_command", return_value='"monitor.exe"'):
            self.assertTrue(autostart.is_autostart_enabled())
            autostart.set_autostart(True)

        winreg.QueryValueEx.assert_called_once_with(read_key, autostart.VALUE_NAME)
        winreg.SetValueEx.assert_called_once_with(
            write_key,
            autostart.VALUE_NAME,
            0,
            winreg.REG_SZ,
            '"monitor.exe"',
        )

    def test_windows_autostart_handles_missing_value_on_read_and_delete(self) -> None:
        winreg = fake_winreg()
        winreg.OpenKey.side_effect = FileNotFoundError

        with patch.object(autostart.os, "name", "nt"), patch.dict(
            sys.modules, {"winreg": winreg}
        ):
            self.assertFalse(autostart.is_autostart_enabled())
            autostart.set_autostart(False)


if __name__ == "__main__":
    unittest.main()
