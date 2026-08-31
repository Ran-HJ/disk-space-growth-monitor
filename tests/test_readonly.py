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
from disk_monitor.scanner import scan_path
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
    def test_snapshot_list_filters_source_time_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            storage = Storage(base / "monitor.db")
            old_result = scan_result(root, 10)
            old_result.started_at = datetime(2026, 1, 1, 8, 0, 0)
            old_result.finished_at = datetime(2026, 1, 1, 8, 0, 0)
            old_id = storage.save_scan(old_result, source="baseline")
            new_result = scan_result(root, 20)
            new_result.started_at = datetime(2026, 1, 2, 8, 0, 0)
            new_result.finished_at = datetime(2026, 1, 2, 8, 0, 0)
            new_id = storage.save_scan(new_result, source="closing")
            queries = ReadOnlyDatabase(base / "monitor.db")

            closing = queries.list_snapshots(source="closing", limit=10)
            recent = queries.list_snapshots(
                finished_after="2026-01-02T00:00:00", limit=10
            )
            second_page = queries.list_snapshots(limit=1, cursor=1)
            with self.assertRaisesRegex(ControlError, "ISO"):
                queries.list_snapshots(finished_after="not-a-date", limit=10)

        self.assertEqual([item["id"] for item in closing], [new_id])
        self.assertEqual([item["id"] for item in recent], [new_id])
        self.assertEqual([item["id"] for item in second_page], [old_id])

    def test_deep_tree_and_comparison_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = base / "monitor.db"
            root = base / "scan-root"
            branch = root / "branch"
            deep = branch / "deep"
            deep.mkdir(parents=True)
            data_file = deep / "data.bin"
            data_file.write_bytes(b"x" * 10)
            storage = Storage(database_path)
            old_result = scan_path(
                str(root), record_depth=1, top_file_limit=0
            )
            old_id = storage.save_scan(old_result, source="baseline")
            data_file.write_bytes(b"x" * 40)
            new_result = scan_path(
                str(root), record_depth=1, top_file_limit=0
            )
            new_id = storage.save_scan(new_result, source="closing")

            queries = ReadOnlyDatabase(database_path)
            tree = queries.snapshot_tree(new_id, str(deep), limit=10)
            comparison = queries.compare_directory_history(
                new_id, old_id, str(branch), limit=10
            )

        self.assertEqual(tree["data_source"], "full_directory_metrics")
        self.assertEqual(tree["items"][0]["kind"], "aggregate")
        self.assertEqual(tree["items"][0]["size_bytes"], 40)
        self.assertEqual(comparison["config_comparison"]["status"], "compatible")
        self.assertEqual(comparison["items"][0]["name"], "deep")
        self.assertEqual(comparison["items"][0]["change_bytes"], 30)

    def test_config_mismatch_blocks_growth_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = base / "monitor.db"
            root = base / "scan-root"
            root.mkdir()
            (root / "data.bin").write_bytes(b"x")
            storage = Storage(database_path)
            old_id = storage.save_scan(
                scan_path(str(root), top_file_limit=0), source="baseline"
            )
            new_id = storage.save_scan(
                scan_path(str(root), top_file_limit=1), source="closing"
            )
            queries = ReadOnlyDatabase(database_path)

            with self.assertRaisesRegex(ControlError, "扫描配置不同") as caught:
                queries.compare_snapshots(new_id, old_id)

        self.assertEqual(caught.exception.code, "scan_config_mismatch")

    def test_snapshot_search_filters_and_paginates_with_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            database_path = base / "monitor.db"
            root = base / "scan-root"
            branch = root / "branch"
            branch.mkdir(parents=True)
            (branch / "large.bin").write_bytes(b"x" * 30)
            (branch / "small.bin").write_bytes(b"x" * 10)
            (branch / "note.txt").write_bytes(b"x" * 20)
            storage = Storage(database_path)
            snapshot_id = storage.save_scan(
                scan_path(str(root)), source="baseline"
            )
            queries = ReadOnlyDatabase(database_path)

            first_page = queries.search_snapshot(
                snapshot_id,
                "branch",
                mode="prefix",
                extension="bin",
                min_size=10,
                limit=1,
            )
            second_page = queries.search_snapshot(
                snapshot_id,
                "branch",
                mode="prefix",
                extension="bin",
                min_size=10,
                limit=1,
                cursor=first_page["next_cursor"],
            )
            largest = queries.largest_snapshot_files(snapshot_id, limit=2)

            with self.assertRaisesRegex(ControlError, "至少需要 3"):
                queries.search_snapshot(snapshot_id, "ab", mode="substring")

        self.assertEqual(first_page["items"][0]["name"], "large.bin")
        self.assertEqual(first_page["next_cursor"], 1)
        self.assertEqual(second_page["items"][0]["name"], "small.bin")
        self.assertIsNone(second_page["next_cursor"])
        self.assertEqual(
            [item["name"] for item in largest["items"]],
            ["large.bin", "note.txt"],
        )
        self.assertIn("Top N", largest["coverage"])

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

            invalid_limit_queries = (
                lambda: queries.session_growth(session_id, limit=0),
                lambda: queries.list_snapshots(limit=False),
                lambda: queries.compare_snapshots(end_id, start_id, limit=-1),
                lambda: queries.snapshot_tree(end_id, str(root), limit=0),
            )
            for query in invalid_limit_queries:
                with self.subTest(query=query):
                    with self.assertRaisesRegex(ControlError, "limit"):
                        query()

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
