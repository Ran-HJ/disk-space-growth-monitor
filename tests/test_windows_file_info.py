from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from disk_monitor.windows_file_info import (
    _extended_path,
    file_information_api_status,
    read_file_space_info,
)


class WindowsFileInfoTests(unittest.TestCase):
    def test_api_status_is_unavailable_off_windows(self) -> None:
        with patch("disk_monitor.windows_file_info.os.name", "posix"):
            status, detail = file_information_api_status()

        self.assertEqual(status, "unavailable")
        self.assertTrue(detail)

    def test_api_status_does_not_open_a_user_file(self) -> None:
        with (
            patch("disk_monitor.windows_file_info.os.name", "nt"),
            patch(
                "disk_monitor.windows_file_info._configure_kernel32",
                return_value=object(),
            ) as configure,
        ):
            status, detail = file_information_api_status()

        self.assertEqual(status, "ok")
        self.assertTrue(detail)
        configure.assert_called_once_with()

    def test_non_windows_returns_explicit_unavailable_state(self) -> None:
        with patch("disk_monitor.windows_file_info.os.name", "posix"):
            information = read_file_space_info("unused")

        self.assertEqual(information.state, "not_windows")
        self.assertIsNone(information.allocated_size_bytes)
        self.assertIsNone(information.identity_key)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供 Win32 文件信息")
    def test_regular_file_returns_all_exact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "普通文件.bin"
            path.write_bytes(b"x" * 4097)

            information = read_file_space_info(
                str(path), expected_size_bytes=path.stat().st_size
            )

        self.assertEqual(information.state, "exact")
        self.assertIsNotNone(information.allocated_size_bytes)
        assert information.allocated_size_bytes is not None
        self.assertGreaterEqual(information.allocated_size_bytes, 4097)
        self.assertEqual(len(information.volume_serial_hex or ""), 16)
        self.assertEqual(len(information.file_id or b""), 16)
        self.assertGreaterEqual(information.link_count or 0, 1)
        self.assertIsNotNone(information.identity_key)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供 Win32 文件信息")
    def test_hard_link_paths_share_identity_and_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "original.bin"
            alias = Path(temp_dir) / "alias.bin"
            original.write_bytes(b"x" * 8193)
            os.link(original, alias)

            original_info = read_file_space_info(str(original))
            alias_info = read_file_space_info(str(alias))

        self.assertEqual(original_info.state, "exact")
        self.assertEqual(alias_info.state, "exact")
        self.assertEqual(original_info.identity_key, alias_info.identity_key)
        self.assertEqual(
            original_info.allocated_size_bytes,
            alias_info.allocated_size_bytes,
        )
        self.assertGreaterEqual(original_info.link_count or 0, 2)
        self.assertGreaterEqual(alias_info.link_count or 0, 2)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供 Win32 文件信息")
    def test_expected_size_mismatch_is_not_reported_as_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "changed.bin"
            path.write_bytes(b"content")

            information = read_file_space_info(
                str(path), expected_size_bytes=1
            )

        self.assertEqual(information.state, "changed_during_scan")
        self.assertIsNotNone(information.identity_key)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供 Win32 文件信息")
    def test_missing_path_returns_error_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.bin"
            information = read_file_space_info(str(missing))

        self.assertEqual(information.state, "inaccessible")
        self.assertIsNotNone(information.error_code)

    @unittest.skipUnless(os.name == "nt", "Windows 路径格式测试")
    def test_extended_path_preserves_existing_prefix(self) -> None:
        path = r"\\?\C:\already-extended.bin"
        self.assertEqual(_extended_path(path), path)


if __name__ == "__main__":
    unittest.main()
