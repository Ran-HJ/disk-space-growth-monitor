from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from disk_monitor.cli import main
from disk_monitor.diagnostics import _process_exists, inspect_doctor
from disk_monitor.models import ScanItem, ScanResult
from disk_monitor.storage import Storage


def saved_scan(root: Path) -> ScanResult:
    now = datetime.now()
    return ScanResult(
        root_path=str(root),
        started_at=now,
        finished_at=now,
        total_bytes=7,
        file_count=1,
        directory_count=1,
        error_count=0,
        items=[
            ScanItem(str(root), str(root.parent), root.name, "directory", 7, 1, 0),
            ScanItem(
                str(root / "data.bin"),
                str(root),
                "data.bin",
                "file",
                7,
                1,
                1,
            ),
        ],
    )


class DiagnosticsTests(unittest.TestCase):
    def run_cli(self, arguments: list[str]) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        self.assertEqual(stderr.getvalue(), "")
        return exit_code, json.loads(stdout.getvalue())

    def test_doctor_checks_a_saved_database_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = base / "monitor.db"
            storage = Storage(database_path)
            storage.save_scan(saved_scan(base / "root"), source="closing")
            before = (database_path.stat().st_size, database_path.stat().st_mtime_ns)

            code, response = self.run_cli(
                [
                    "doctor",
                    "--database",
                    str(database_path),
                    "--control-directory",
                    str(base / "control"),
                    "--json",
                ]
            )
            after = (database_path.stat().st_size, database_path.stat().st_mtime_ns)

        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        data = response["data"]
        assert isinstance(data, dict)
        self.assertEqual(
            set(data),
            {
                "version",
                "protocol_version",
                "overall_status",
                "database",
                "control",
                "logging",
                "file_information",
                "latest_scan",
                "checks",
            },
        )
        self.assertEqual(data["database"]["status"], "ok")
        self.assertTrue(data["database"]["read_only"])
        self.assertEqual(data["database"]["quick_check"], "ok")
        self.assertEqual(data["database"]["foreign_key_check"], "ok")
        self.assertEqual(data["latest_scan"]["source"], "closing")
        self.assertIn(
            data["overall_status"], {"ok", "warning", "error", "unavailable"}
        )
        for check in data["checks"].values():
            self.assertIn(
                check["status"], {"ok", "warning", "error", "unavailable"}
            )
        self.assertEqual(before, after)
        self.assertNotIn("auth_file", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("pipe_address", json.dumps(data, ensure_ascii=False))

    def test_missing_or_corrupt_database_never_creates_or_repairs_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            missing = base / "missing" / "monitor.db"
            missing_report = inspect_doctor(database_path=missing)
            self.assertEqual(missing_report["database"]["status"], "unavailable")
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

            corrupt = base / "corrupt.db"
            corrupt.write_bytes(b"not a sqlite database")
            before = (corrupt.stat().st_size, corrupt.stat().st_mtime_ns)
            corrupt_report = inspect_doctor(database_path=corrupt)
            after = (corrupt.stat().st_size, corrupt.stat().st_mtime_ns)

            directory_report = inspect_doctor(database_path=base)

        self.assertEqual(corrupt_report["database"]["status"], "error")
        self.assertEqual(before, after)
        self.assertEqual(directory_report["database"]["status"], "error")

    def test_foreign_keys_are_checked_separately_from_quick_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "foreign-key.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = OFF;
                    CREATE TABLE parent(id INTEGER PRIMARY KEY);
                    CREATE TABLE child(
                        parent_id INTEGER REFERENCES parent(id)
                    );
                    INSERT INTO child(parent_id) VALUES (99);
                    """
                )
                connection.commit()
            finally:
                connection.close()
            before = (database_path.stat().st_size, database_path.stat().st_mtime_ns)

            report = inspect_doctor(database_path=database_path)
            after = (database_path.stat().st_size, database_path.stat().st_mtime_ns)

        database = report["database"]
        self.assertEqual(database["status"], "error")
        self.assertEqual(database["quick_check"], "ok")
        self.assertEqual(database["foreign_key_check"], "error")
        self.assertEqual(database["foreign_key_issue_count"], 1)
        self.assertEqual(before, after)

    @unittest.skipUnless(os.name == "nt", "Windows 进程探测回归")
    def test_windows_process_probe_never_sends_a_signal(self) -> None:
        with patch("disk_monitor.diagnostics.os.kill") as kill:
            self.assertTrue(_process_exists(os.getpid()))

        kill.assert_not_called()

    def test_control_summary_never_exposes_endpoint_authentication_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            endpoint = {
                "protocol_version": "1",
                "instance_id": "a" * 32,
                "pid": os.getpid(),
                "pipe_address": r"\\.\pipe\DiskGrowthMonitor-fixture",
                "auth_file": "control-fixture.auth",
            }
            (directory / "control.endpoint.json").write_text(
                json.dumps(endpoint), encoding="utf-8"
            )
            (directory / "control-fixture.auth").write_bytes(b"secret-fixture")
            endpoint_path = directory / "control.endpoint.json"
            old_timestamp = endpoint_path.stat().st_mtime - 3600
            os.utime(endpoint_path, (old_timestamp, old_timestamp))
            auth_before = (
                (directory / "control-fixture.auth").stat().st_size,
                (directory / "control-fixture.auth").stat().st_mtime_ns,
            )
            report = inspect_doctor(
                database_path=directory / "missing.db",
                control_directory=directory,
            )
            auth_after = (
                (directory / "control-fixture.auth").stat().st_size,
                (directory / "control-fixture.auth").stat().st_mtime_ns,
            )

        control = report["control"]
        self.assertTrue(control["endpoint_present"])
        self.assertTrue(control["process_alive"])
        self.assertFalse(control["endpoint_stale"])
        self.assertEqual(auth_before, auth_after)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("auth_file", rendered)
        self.assertNotIn("pipe_address", rendered)
        self.assertNotIn("secret-fixture", rendered)


if __name__ == "__main__":
    unittest.main()
