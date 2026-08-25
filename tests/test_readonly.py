from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from disk_monitor.control_protocol import ControlError
from disk_monitor.models import DiskSample, ScanItem, ScanResult
from disk_monitor.readonly import ReadOnlyDatabase
from disk_monitor.storage import Storage


def scan_result(root: Path, size: int) -> ScanResult:
    now = datetime.now()
    normalized_root = str(root)
    file_path = str(root / "data.bin")
    return ScanResult(
        root_path=normalized_root,
        started_at=now,
        finished_at=now,
        total_bytes=size,
        file_count=1,
        directory_count=1,
        error_count=0,
        items=[
            ScanItem(
                normalized_root,
                str(root.parent),
                root.name,
                "directory",
                size,
                1,
                0,
            ),
            ScanItem(
                file_path,
                normalized_root,
                "data.bin",
                "file",
                size,
                1,
                1,
            ),
        ],
    )


class ReadOnlyDatabaseTests(unittest.TestCase):
    def test_session_growth_snapshots_and_tree_are_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = base / "monitor.db"
            root = base / "scan-root"
            storage = Storage(database_path)
            start_id = storage.save_scan(scan_result(root, 10), source="baseline")
            end_id = storage.save_scan(scan_result(root, 30), source="closing")
            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            session_id = storage.start_session(sample, str(root))
            storage.set_session_start_snapshot(session_id, start_id)
            storage.finish_session(
                session_id,
                DiskSample(datetime.now(), "C:\\", 1000, 425, 575),
                end_snapshot_id=end_id,
                end_reason="normal_close",
            )

            queries = ReadOnlyDatabase(database_path)
            session = queries.latest_completed_session()
            growth = queries.session_growth(session_id)
            snapshots = queries.list_snapshots(limit=10)
            tree = queries.snapshot_tree(end_id, str(root), limit=10)

            self.assertEqual(session["id"], session_id)
            self.assertEqual(session["change_bytes"], 25)
            self.assertEqual(growth[0]["change_bytes"], 20)
            self.assertEqual([item["id"] for item in snapshots], [end_id, start_id])
            self.assertEqual(tree["items"][0]["name"], "data.bin")

    def test_connection_is_query_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            Storage(database_path)
            queries = ReadOnlyDatabase(database_path)

            with queries.connection() as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(
                        "INSERT INTO app_settings(key, value) VALUES ('x', 'y')"
                    )

    def test_missing_and_future_databases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            with self.assertRaisesRegex(ControlError, "不存在"):
                with ReadOnlyDatabase(base / "missing.db").connection():
                    pass

            future_path = base / "future.db"
            with closing(sqlite3.connect(future_path)) as connection:
                connection.execute("PRAGMA user_version = 99")
            with self.assertRaisesRegex(ControlError, "版本"):
                with ReadOnlyDatabase(future_path).connection():
                    pass


if __name__ == "__main__":
    unittest.main()
