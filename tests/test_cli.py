from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from disk_monitor.cli import main
from disk_monitor.control_protocol import error_response, success_response
from disk_monitor.models import DiskSample, ScanItem, ScanResult
from disk_monitor.storage import Storage


def make_scan(root: Path, size: int) -> ScanResult:
    now = datetime.now()
    return ScanResult(
        root_path=str(root),
        started_at=now,
        finished_at=now,
        total_bytes=size,
        file_count=1,
        directory_count=1,
        error_count=0,
        items=[
            ScanItem(str(root), str(root.parent), root.name, "directory", size, 1, 0),
            ScanItem(
                str(root / "data.bin"),
                str(root),
                "data.bin",
                "file",
                size,
                1,
                1,
            ),
        ],
    )


class CliTests(unittest.TestCase):
    def run_cli(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def create_history(self, base: Path) -> Path:
        database_path = base / "monitor.db"
        root = base / "root"
        storage = Storage(database_path)
        start_id = storage.save_scan(make_scan(root, 10), source="baseline")
        end_id = storage.save_scan(make_scan(root, 25), source="closing")
        sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
        session_id = storage.start_session(sample, str(root))
        storage.set_session_start_snapshot(session_id, start_id)
        storage.finish_session(
            session_id,
            DiskSample(datetime.now(), "C:\\", 1000, 430, 570),
            end_snapshot_id=end_id,
            end_reason="normal_close",
        )
        return database_path

    def test_snapshot_list_and_growth_last_emit_json_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self.create_history(Path(temp_dir))

            code, output, _ = self.run_cli(
                [
                    "snapshot",
                    "list",
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )
            snapshot_response = json.loads(output)
            self.assertEqual(code, 0)
            self.assertTrue(snapshot_response["ok"])
            self.assertEqual(len(snapshot_response["data"]["snapshots"]), 2)

            code, output, _ = self.run_cli(
                [
                    "growth",
                    "last",
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )
            growth_response = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(growth_response["data"]["session"]["change_bytes"], 30)
            self.assertEqual(growth_response["data"]["items"][0]["change_bytes"], 15)

    def test_snapshot_accounting_compare_reports_legacy_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self.create_history(Path(temp_dir))
            snapshots = Storage(database_path).list_snapshots()

            code, output, _ = self.run_cli(
                [
                    "snapshot",
                    "compare",
                    str(snapshots[0].id),
                    str(snapshots[1].id),
                    "--accounting",
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )

        response = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        self.assertFalse(response["data"]["available"])
        self.assertEqual(
            response["data"]["reason"],
            "measurement_coverage_incomplete",
        )

    def test_snapshot_search_and_largest_are_bounded_read_only_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self.create_history(Path(temp_dir))

            search_code, search_output, _ = self.run_cli(
                [
                    "snapshot",
                    "search",
                    "2",
                    "data.bin",
                    "--mode",
                    "substring",
                    "--limit",
                    "1",
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )
            largest_code, largest_output, _ = self.run_cli(
                [
                    "snapshot",
                    "largest",
                    "2",
                    "--limit",
                    "1",
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )

        search_response = json.loads(search_output)
        largest_response = json.loads(largest_output)
        self.assertEqual(search_code, 0)
        self.assertEqual(largest_code, 0)
        self.assertEqual(search_response["data"]["items"][0]["name"], "data.bin")
        self.assertEqual(largest_response["data"]["items"][0]["name"], "data.bin")
        self.assertIn("Top N", largest_response["data"]["coverage"])

    def test_snapshot_migration_advice_has_notice_and_no_action_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = self.create_history(base)

            code, output, _ = self.run_cli(
                [
                    "advice",
                    "list",
                    "--snapshot-id",
                    "2",
                    "--target",
                    str(base),
                    "--database",
                    str(database_path),
                    "--json",
                ]
            )

        response = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        self.assertIn("不会移动或修改文件", response["data"]["notice"])
        self.assertNotIn("action", response["data"])

    def test_runtime_mode_command_is_sent_to_gui(self) -> None:
        client = Mock()
        client.request.return_value = success_response(
            "request", data={"mode": "low_memory"}
        )
        with patch("disk_monitor.cli.ControlClient", return_value=client):
            code, output, _ = self.run_cli(
                ["mode", "set", "low_memory", "--json"]
            )

        response = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(response["data"]["mode"], "low_memory")
        client.request.assert_called_once_with(
            "mode.set", {"mode": "low_memory", "rescan": None}
        )

    def test_app_status_treats_unavailable_gui_as_not_running(self) -> None:
        client = Mock()
        client.request.return_value = error_response(
            "request", "gui_unavailable", "GUI 控制服务不可用"
        )
        with patch("disk_monitor.cli.ControlClient", return_value=client):
            code, output, _ = self.run_cli(["app", "status", "--json"])

        response = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["code"], "not_running")
        self.assertFalse(response["data"]["running"])

    def test_automation_configuration_is_sent_to_gui(self) -> None:
        client = Mock()
        client.request.return_value = success_response(
            "request", data={"status": "monitoring"}
        )
        with patch("disk_monitor.cli.ControlClient", return_value=client):
            code, output, _ = self.run_cli(
                [
                    "automation",
                    "configure",
                    "--enabled",
                    "on",
                    "--processes",
                    "cs2;r5apex.exe",
                    "--memory-pressure",
                    "on",
                    "--high",
                    "88",
                    "--low",
                    "72",
                    "--resume-rescan",
                    "later",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["data"]["status"], "monitoring")
        client.request.assert_called_once_with(
            "automation.configure",
            {
                "enabled": True,
                "process_names": "cs2;r5apex.exe",
                "memory_pressure_enabled": True,
                "high_percent": 88,
                "low_percent": 72,
                "resume_rescan": "later",
            },
        )

    def test_json_stdout_is_utf8_when_parent_requests_gbk(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "gbk"
        with tempfile.TemporaryDirectory() as temp_dir:
            environment["LOCALAPPDATA"] = temp_dir
            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "run_cli.py"),
                    "app",
                    "status",
                    "--json",
                ],
                cwd=project_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = completed.stdout.decode("utf-8")
        response = json.loads(output)
        self.assertEqual(response["message"], "GUI 未运行")
        self.assertEqual(completed.stderr, b"")


if __name__ == "__main__":
    unittest.main()
