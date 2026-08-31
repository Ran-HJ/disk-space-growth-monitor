from __future__ import annotations

import os
import tempfile
import unittest
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from disk_monitor.models import DiskSample, ScanItem, ScanResult
from disk_monitor.readonly import ReadOnlyDatabase
from disk_monitor.scanner import scan_path
from disk_monitor.storage import CURRENT_SCHEMA_VERSION, Storage


def make_result(root: str, size: int) -> ScanResult:
    now = datetime.now()
    file_path = str(Path(root) / "growing.bin")
    return ScanResult(
        root_path=root,
        started_at=now,
        finished_at=now,
        total_bytes=size,
        file_count=1,
        directory_count=1,
        error_count=0,
        items=[
            ScanItem(root, str(Path(root).parent), Path(root).name, "directory", size, 1, 0),
            ScanItem(file_path, root, "growing.bin", "file", size, 1, 1),
        ],
    )


def create_v1_database(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE disk_samples (
                id INTEGER PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                drive TEXT NOT NULL,
                total_bytes INTEGER NOT NULL,
                used_bytes INTEGER NOT NULL,
                free_bytes INTEGER NOT NULL
            );
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                total_bytes INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                directory_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                note TEXT,
                source TEXT
            );
            CREATE TABLE snapshot_items (
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                PRIMARY KEY(snapshot_id, path)
            );
            CREATE TABLE monitor_sessions (
                id INTEGER PRIMARY KEY,
                drive TEXT NOT NULL,
                root_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_used_bytes INTEGER NOT NULL,
                end_used_bytes INTEGER,
                start_snapshot_id INTEGER,
                end_snapshot_id INTEGER,
                end_reason TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE session_growth_items (
                session_id INTEGER NOT NULL REFERENCES monitor_sessions(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                old_size_bytes INTEGER NOT NULL,
                new_size_bytes INTEGER NOT NULL,
                change_bytes INTEGER NOT NULL,
                PRIMARY KEY(session_id, path)
            );
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO snapshots(
                id, root_path, started_at, finished_at, total_bytes,
                file_count, directory_count, error_count, note, source
            ) VALUES (
                1, 'C:\\fixture', '2026-08-01T00:00:00',
                '2026-08-01T00:00:01', 10, 1, 1, 0, NULL, 'manual'
            );
            INSERT INTO snapshot_items(
                snapshot_id, path, parent_path, name, kind, size_bytes,
                file_count, depth, modified_at
            ) VALUES (
                1, 'C:\\fixture', 'C:\\', 'fixture', 'directory',
                10, 1, 0, 0
            );
            PRAGMA user_version = 1;
            """
        )


def create_v2_database(database_path: Path) -> None:
    create_v1_database(database_path)
    snapshot_columns = {
        "allocated_total_bytes": "INTEGER",
        "unique_allocated_total_bytes": "INTEGER",
        "measured_allocated_bytes": "INTEGER NOT NULL DEFAULT 0",
        "measured_unique_allocated_bytes": "INTEGER NOT NULL DEFAULT 0",
        "eligible_file_count": "INTEGER NOT NULL DEFAULT 0",
        "allocation_measured_file_count": "INTEGER NOT NULL DEFAULT 0",
        "identity_measured_file_count": "INTEGER NOT NULL DEFAULT 0",
        "metadata_error_count": "INTEGER NOT NULL DEFAULT 0",
        "measurement_state": "TEXT NOT NULL DEFAULT 'legacy'",
    }
    item_columns = {
        "allocated_size_bytes": "INTEGER",
        "unique_allocated_size_bytes": "INTEGER",
        "volume_serial_hex": "TEXT",
        "file_id": "BLOB",
        "link_count": "INTEGER",
        "is_unique_owner": "INTEGER",
        "measurement_state": "TEXT NOT NULL DEFAULT 'legacy'",
    }
    with closing(sqlite3.connect(database_path)) as connection:
        for name, definition in snapshot_columns.items():
            connection.execute(
                f"ALTER TABLE snapshots ADD COLUMN {name} {definition}"
            )
        for name, definition in item_columns.items():
            connection.execute(
                f"ALTER TABLE snapshot_items ADD COLUMN {name} {definition}"
            )
        connection.execute("PRAGMA user_version = 2")


class StorageTests(unittest.TestCase):
    def test_new_database_records_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "test.db"

            Storage(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    def test_database_newer_than_supported_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "future.db"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA user_version = 99")

            with self.assertRaisesRegex(RuntimeError, "数据库版本"):
                Storage(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal_mode, "delete")

    def test_disk_samples_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            sample = DiskSample(datetime.now(), "C:\\", 1000, 600, 400)
            storage.add_disk_sample(sample)
            samples = storage.get_disk_samples("C:\\")
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].used_bytes, 600)

    def test_session_boundaries_are_filtered_by_drive_and_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            now = datetime.now()
            started = DiskSample(
                now - timedelta(hours=2), "C:\\", 1000, 400, 600
            )
            ended = DiskSample(
                now - timedelta(minutes=30), "C:\\", 1000, 410, 590
            )
            session_id = storage.start_session(started, "C:\\")
            storage.finish_session(session_id, ended, end_reason="quick_close")
            other_drive = DiskSample(now, "D:\\", 1000, 300, 700)
            storage.start_session(other_drive, "D:\\")

            boundaries = storage.get_session_boundaries("C:\\", hours=3)
            recent_boundaries = storage.get_session_boundaries("C:\\", hours=1)

            self.assertEqual(
                [boundary.kind for boundary in boundaries], ["start", "end"]
            )
            self.assertEqual(
                [boundary.kind for boundary in recent_boundaries], ["end"]
            )

    def test_latest_disk_sample_is_read_before_retention_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            now = datetime.now()
            old_sample = DiskSample(
                now - timedelta(days=40), "C:\\", 1000, 550, 450
            )
            storage.add_disk_sample(old_sample)

            latest = storage.get_latest_disk_sample("C:\\")
            deleted = storage.prune_disk_samples(retention_days=30, now=now)

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.used_bytes, 550)
            self.assertEqual(deleted, 1)

    def test_snapshot_comparison_reports_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            first = make_result(root, 10)
            second = make_result(root, 25)
            first_id = storage.save_scan(first)
            second_id = storage.save_scan(second)

            growth = storage.compare_snapshots(second_id, first_id)

            file_growth = next(item for item in growth if item.name == "growing.bin")
            self.assertEqual(file_growth.change_bytes, 15)
            self.assertEqual(storage.previous_snapshot_id(second), first_id)

    def test_load_snapshot_rebuilds_result_and_sorts_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            result = make_result(str(Path(temp_dir) / "root"), 25)
            result.items.reverse()
            snapshot_id = storage.save_scan(result)

            loaded = storage.load_snapshot(snapshot_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.snapshot_id, snapshot_id)
            self.assertEqual(loaded.root_path, result.root_path)
            self.assertEqual(loaded.total_bytes, result.total_bytes)
            self.assertEqual(
                [item.path for item in loaded.items],
                sorted(item.path for item in result.items),
            )
            self.assertIsNone(storage.load_snapshot(snapshot_id + 999))

    def test_marked_snapshots_are_listed_before_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            first_id = storage.save_scan(make_result(root, 10), note="清理前")
            storage.save_scan(make_result(root, 15))
            current_id = storage.save_scan(make_result(root, 20), note="清理后")

            snapshots = storage.list_marked_snapshots(
                root, before_snapshot_id=current_id
            )

            self.assertEqual([item.id for item in snapshots], [first_id])
            self.assertEqual(snapshots[0].note, "清理前")

    def test_existing_database_is_migrated_with_snapshot_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "old.db"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE snapshots (
                        id INTEGER PRIMARY KEY,
                        root_path TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        total_bytes INTEGER NOT NULL,
                        file_count INTEGER NOT NULL,
                        directory_count INTEGER NOT NULL,
                        error_count INTEGER NOT NULL
                    )
                    """
                )

            Storage(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(snapshots)")
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("note", columns)
            self.assertIn("source", columns)
            self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    def test_v1_migration_creates_and_reuses_one_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            create_v1_database(database_path)

            storage = Storage(database_path)

            self.assertIsNotNone(storage.migration_backup_path)
            assert storage.migration_backup_path is not None
            self.assertTrue(storage.migration_backup_path.is_file())
            backup_files = list(
                (Path(temp_dir) / "backups").glob(
                    "monitor-v1-before-v2-*.db"
                )
            )
            self.assertEqual(backup_files, [storage.migration_backup_path])
            with closing(
                sqlite3.connect(storage.migration_backup_path)
            ) as backup:
                self.assertEqual(
                    backup.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    backup.execute("PRAGMA user_version").fetchone()[0], 1
                )
                self.assertEqual(
                    backup.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                    1,
                )
            loaded = storage.load_snapshot(1)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.measurement_state, "legacy")
            self.assertIsNone(loaded.allocated_total_bytes)
            readonly = ReadOnlyDatabase(database_path)
            self.assertEqual(len(readonly.list_snapshots(limit=5)), 1)
            self.assertIsNotNone(readonly.snapshot_info(1))
            self.assertEqual(
                readonly.get_setting("schema_v2_backup_path"),
                str(storage.migration_backup_path),
            )

            reopened = Storage(database_path)

            self.assertIsNone(reopened.migration_backup_path)
            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v1-before-v2-*.db"
                        )
                    )
                ),
                1,
            )

    def test_v2_migration_creates_one_backup_and_v3_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            create_v2_database(database_path)

            storage = Storage(database_path)

            self.assertIsNotNone(storage.migration_backup_path)
            assert storage.migration_backup_path is not None
            self.assertEqual(
                list((Path(temp_dir) / "backups").glob("monitor-v2-before-v3-*.db")),
                [storage.migration_backup_path],
            )
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 3
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                legacy_state = connection.execute(
                    "SELECT directory_summary_state FROM snapshots WHERE id = 1"
                ).fetchone()[0]
            self.assertIn("directory_paths", tables)
            self.assertIn("snapshot_directory_metrics", tables)
            self.assertEqual(legacy_state, "legacy")
            self.assertEqual(
                ReadOnlyDatabase(database_path).get_setting(
                    "schema_v3_backup_path"
                ),
                str(storage.migration_backup_path),
            )

            reopened = Storage(database_path)

            self.assertIsNone(reopened.migration_backup_path)
            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v2-before-v3-*.db"
                        )
                    )
                ),
                1,
            )

    def test_failed_v2_migration_rolls_back_and_reuses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            create_v2_database(database_path)

            with patch.object(
                Storage,
                "_apply_schema_v3",
                side_effect=RuntimeError("injected v3 migration failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected v3 migration failure"
                ):
                    Storage(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(snapshots)")
                }
            self.assertEqual(version, 2)
            self.assertNotIn("directory_paths", tables)
            self.assertNotIn("snapshot_directory_metrics", tables)
            self.assertNotIn("scan_config_version", columns)
            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v2-before-v3-*.db"
                        )
                    )
                ),
                1,
            )

            Storage(database_path)

            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v2-before-v3-*.db"
                        )
                    )
                ),
                1,
            )

    def test_failed_v1_migration_rolls_back_and_reuses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            create_v1_database(database_path)

            with patch.object(
                Storage,
                "_apply_schema_v2",
                side_effect=RuntimeError("injected migration failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected migration failure"
                ):
                    Storage(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(snapshots)")
                }
            self.assertEqual(version, 1)
            self.assertNotIn("allocated_total_bytes", columns)
            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v1-before-v2-*.db"
                        )
                    )
                ),
                1,
            )

            Storage(database_path)

            self.assertEqual(
                len(
                    list(
                        (Path(temp_dir) / "backups").glob(
                            "monitor-v1-before-v2-*.db"
                        )
                    )
                ),
                1,
            )

    def test_file_space_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            result = make_result(str(Path(temp_dir) / "root"), 25)
            result.allocated_total_bytes = 4096
            result.unique_allocated_total_bytes = 4096
            result.measured_allocated_bytes = 4096
            result.measured_unique_allocated_bytes = 4096
            result.eligible_file_count = 1
            result.allocation_measured_file_count = 1
            result.identity_measured_file_count = 1
            result.measurement_state = "exact"
            result.items = [
                replace(
                    item,
                    allocated_size_bytes=4096,
                    unique_allocated_size_bytes=4096,
                    volume_serial_hex="000000001234abcd",
                    file_id=bytes(range(16)),
                    link_count=2,
                    is_unique_owner=True,
                    measurement_state="exact",
                )
                if item.kind == "file"
                else item
                for item in result.items
            ]

            snapshot_id = storage.save_scan(result)
            loaded = storage.load_snapshot(snapshot_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.allocated_total_bytes, 4096)
        self.assertEqual(loaded.unique_allocated_total_bytes, 4096)
        self.assertEqual(loaded.measurement_state, "exact")
        loaded_file = next(
            item for item in loaded.items if item.kind == "file"
        )
        self.assertEqual(loaded_file.file_id, bytes(range(16)))
        self.assertEqual(loaded_file.link_count, 2)
        self.assertTrue(loaded_file.is_unique_owner)

    def test_full_directory_metrics_reuse_paths_and_round_trip_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            deep = root / "first" / "deep"
            second = root / "second"
            deep.mkdir(parents=True)
            second.mkdir()
            (deep / "data.bin").write_bytes(b"x" * 128)
            (second / "other.bin").write_bytes(b"y" * 64)
            storage = Storage(base / "monitor.db")
            result = scan_path(str(root), record_depth=1)
            result.scan_config_version = 1
            result.scan_config_json = '{"collect_file_space":false}'

            baseline_id = storage.save_scan(result, source="baseline")
            closing_id = storage.save_scan(result, source="closing")
            manual_id = storage.save_scan(result, source="manual")
            loaded = storage.load_snapshot(baseline_id)

            with closing(sqlite3.connect(base / "monitor.db")) as connection:
                path_count = connection.execute(
                    "SELECT COUNT(*) FROM directory_paths"
                ).fetchone()[0]
                metric_counts = {
                    row[0]: row[1]
                    for row in connection.execute(
                        """
                        SELECT snapshot_id, COUNT(*)
                        FROM snapshot_directory_metrics
                        GROUP BY snapshot_id
                        """
                    )
                }
                child_parent = connection.execute(
                    """
                    SELECT parent.path
                    FROM directory_paths AS child
                    JOIN directory_paths AS parent
                        ON parent.id = child.parent_id
                    WHERE child.path = ? COLLATE NOCASE
                    """,
                    (os.path.normcase(os.path.abspath(deep)),),
                ).fetchone()[0]
                summary_states = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT id, directory_summary_state FROM snapshots"
                    )
                }

        self.assertEqual(path_count, result.directory_count)
        self.assertEqual(metric_counts[baseline_id], result.directory_count)
        self.assertEqual(metric_counts[closing_id], result.directory_count)
        self.assertNotIn(manual_id, metric_counts)
        self.assertEqual(
            child_parent,
            os.path.normcase(os.path.abspath(deep.parent)),
        )
        self.assertEqual(summary_states[baseline_id], "complete")
        self.assertEqual(summary_states[closing_id], "complete")
        self.assertEqual(summary_states[manual_id], "not_saved")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.scan_config_version, 1)
        self.assertEqual(
            loaded.scan_config_json, '{"collect_file_space":false}'
        )

    def test_automatic_baseline_only_uses_matching_scan_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            root.mkdir()
            (root / "data.bin").write_bytes(b"x")
            storage = Storage(base / "monitor.db")
            matching = scan_path(str(root), top_file_limit=0)
            old_id = storage.save_scan(matching, source="manual")
            matching_id = storage.save_scan(matching, source="manual")
            different = scan_path(str(root), top_file_limit=1)
            different_id = storage.save_scan(different, source="manual")

            matching.snapshot_id = matching_id
            self.assertEqual(storage.previous_snapshot_id(matching), old_id)
            self.assertIsNone(storage.previous_snapshot_id(different))
            with self.assertRaisesRegex(ValueError, "扫描配置不同"):
                storage.compare_snapshot_changes(
                    different_id,
                    matching_id,
                    direction="increase",
                )

    def test_selected_source_without_skeleton_is_marked_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "monitor.db"
            storage = Storage(database_path)

            snapshot_id = storage.save_scan(
                make_result(str(Path(temp_dir) / "root"), 10),
                source="baseline",
            )

            with closing(sqlite3.connect(database_path)) as connection:
                state = connection.execute(
                    "SELECT directory_summary_state FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()[0]
                metric_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM snapshot_directory_metrics
                    WHERE snapshot_id = ?
                    """,
                    (snapshot_id,),
                ).fetchone()[0]
        self.assertEqual(state, "unavailable")
        self.assertEqual(metric_count, 0)

    def test_directory_metric_retention_preserves_active_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            root.mkdir()
            (root / "data.bin").write_bytes(b"x")
            storage = Storage(base / "monitor.db")
            now = datetime.now().replace(microsecond=0)
            result = scan_path(str(root))
            directory_count = result.directory_count

            def save_old(source: str, days: int) -> int:
                result.started_at = now - timedelta(days=days)
                result.finished_at = now - timedelta(days=days)
                return storage.save_scan(result, source=source)

            expired_baseline = save_old("baseline", 8)
            expired_closing = save_old("closing", 31)
            expired_manual = save_old("manual_save", 91)
            active_baseline = save_old("baseline", 8)
            sample = DiskSample(now, "C:\\", 1000, 400, 600)
            session_id = storage.start_session(sample, str(root))
            storage.set_session_start_snapshot(session_id, active_baseline)

            deleted = storage.prune_directory_metrics(now=now)

            with closing(sqlite3.connect(base / "monitor.db")) as connection:
                remaining = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT snapshot_id FROM snapshot_directory_metrics"
                    )
                }
                states = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT id, directory_summary_state FROM snapshots"
                    )
                }

        self.assertEqual(deleted, directory_count * 3)
        self.assertNotIn(expired_baseline, remaining)
        self.assertNotIn(expired_closing, remaining)
        self.assertNotIn(expired_manual, remaining)
        self.assertIn(active_baseline, remaining)
        self.assertEqual(states[expired_baseline], "expired")
        self.assertEqual(states[active_baseline], "complete")

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供硬链接统计")
    def test_accounting_comparison_does_not_treat_alias_as_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            first_directory = root / "a"
            later_directory = root / "z"
            first_directory.mkdir(parents=True)
            later_directory.mkdir()
            original = later_directory / "original.bin"
            alias = first_directory / "alias.bin"
            original.write_bytes(b"x" * 8193)
            storage = Storage(base / "monitor.db")
            old_id = storage.save_scan(
                scan_path(
                    str(root),
                    record_depth=2,
                    collect_file_space=True,
                )
            )
            os.link(original, alias)
            new_id = storage.save_scan(
                scan_path(
                    str(root),
                    record_depth=2,
                    collect_file_space=True,
                )
            )

            comparison = storage.compare_snapshot_accounting(new_id, old_id)
            readonly_comparison = ReadOnlyDatabase(
                base / "monitor.db"
            ).compare_snapshot_accounting(new_id, old_id)

        self.assertTrue(comparison["available"])
        self.assertEqual(comparison["unique_allocated_total_change_bytes"], 0)
        self.assertEqual(comparison["verified_item_change_bytes"], 0)
        self.assertEqual(comparison["items"][0]["change_bytes"], 0)
        self.assertEqual(
            comparison["items"][0]["change_kind"],
            "accounting_path_changed",
        )
        self.assertEqual(readonly_comparison, comparison)

    def test_snapshot_history_uses_reference_source_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            baseline_id = storage.save_scan(
                make_result(root, 10), note="手动保存", source="manual_save"
            )
            closing_id = storage.save_scan(
                make_result(root, 20), source="manual"
            )
            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            session_id = storage.start_session(sample, root)
            storage.set_session_start_snapshot(session_id, baseline_id)
            storage.finish_session(
                session_id,
                sample,
                end_snapshot_id=closing_id,
                end_reason="normal_close",
            )

            history = {item.id: item for item in storage.list_snapshots()}

            self.assertEqual(history[baseline_id].source, "baseline")
            self.assertEqual(history[closing_id].source, "closing")

    def test_latest_full_snapshot_ignores_newer_quick_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            baseline_id = storage.save_scan(
                make_result(root, 10), source="baseline"
            )
            closing_id = storage.save_scan(
                make_result(root, 20), source="closing"
            )
            full_session = storage.start_session(sample, root)
            storage.set_session_start_snapshot(full_session, baseline_id)
            storage.finish_session(
                full_session,
                sample,
                end_snapshot_id=closing_id,
                end_reason="normal_close",
            )
            quick_session = storage.start_session(sample, root)
            storage.finish_session(
                quick_session,
                sample,
                end_reason="quick_close",
            )

            latest = storage.latest_full_snapshot_for_path(root)

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.id, closing_id)
            self.assertEqual(latest.source, "closing")
            self.assertIsNone(
                storage.latest_full_snapshot_for_path(str(Path(temp_dir) / "other"))
            )

    def test_snapshot_history_cursor_handles_same_second(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            now = datetime.now().replace(microsecond=0)
            ids: list[int] = []
            for size in range(5):
                result = make_result(root, size + 1)
                result.started_at = now
                result.finished_at = now
                ids.append(storage.save_scan(result, source="manual"))

            first_page = storage.list_snapshots(limit=2)
            cursor = (first_page[-1].finished_at, first_page[-1].id)
            second_page = storage.list_snapshots(limit=2, cursor=cursor)
            cursor = (second_page[-1].finished_at, second_page[-1].id)
            third_page = storage.list_snapshots(limit=2, cursor=cursor)
            loaded_ids = [
                item.id for item in first_page + second_page + third_page
            ]

            self.assertEqual(loaded_ids, list(reversed(ids)))
            self.assertEqual(len(loaded_ids), len(set(loaded_ids)))

    def test_prune_disk_samples_keeps_only_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            now = datetime.now()
            storage.add_disk_sample(
                DiskSample(now - timedelta(days=31), "C:\\", 1000, 500, 500)
            )
            storage.add_disk_sample(
                DiskSample(now - timedelta(days=2), "C:\\", 1000, 600, 400)
            )

            deleted = storage.prune_disk_samples(retention_days=30, now=now)

            samples = storage.get_disk_samples("C:\\", hours=24 * 40)
            self.assertEqual(deleted, 1)
            self.assertEqual([sample.used_bytes for sample in samples], [600])

    def test_session_saves_total_and_path_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            started = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            ended = DiskSample(datetime.now(), "C:\\", 1000, 430, 570)
            start_result = make_result(root, 10)
            end_result = make_result(root, 25)
            start_snapshot_id = storage.save_scan(start_result)
            end_snapshot_id = storage.save_scan(end_result)

            session_id = storage.start_session(started, root)
            storage.set_session_start_snapshot(session_id, start_snapshot_id)
            growth = storage.finish_session(
                session_id,
                ended,
                end_snapshot_id=end_snapshot_id,
                end_reason="normal_close",
            )

            session = storage.latest_completed_session()
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.id, session_id)
            self.assertEqual(session.change_bytes, 30)
            self.assertEqual(session.status, "completed")
            self.assertEqual(session.end_reason, "normal_close")
            file_growth = next(item for item in growth if item.name == "growing.bin")
            self.assertEqual(file_growth.change_bytes, 15)
            self.assertEqual(
                storage.get_session_growth(session_id)[0].change_bytes,
                growth[0].change_bytes,
            )

    def test_finish_session_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            session_id = storage.start_session(sample, "C:\\")

            storage.finish_session(session_id, sample, end_reason="normal_close")
            storage.finish_session(session_id, sample, end_reason="process_exit")

            session = storage.latest_completed_session()
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.end_reason, "normal_close")

    def test_finish_session_returns_growth_without_opening_a_second_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            sample = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            start_id = storage.save_scan(make_result(root, 10))
            end_id = storage.save_scan(make_result(root, 20))
            session_id = storage.start_session(sample, root)
            storage.set_session_start_snapshot(session_id, start_id)
            original_connect = storage._connect
            connect_count = 0

            def counted_connect():
                nonlocal connect_count
                connect_count += 1
                return original_connect()

            with patch.object(storage, "_connect", side_effect=counted_connect):
                growth = storage.finish_session(
                    session_id,
                    sample,
                    end_snapshot_id=end_id,
                    end_reason="normal_close",
                )

            self.assertEqual(connect_count, 1)
            self.assertTrue(growth)

    def test_unfinished_session_is_recovered_on_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            started = DiskSample(datetime.now(), "C:\\", 1000, 400, 600)
            recovered = DiskSample(datetime.now(), "C:\\", 1000, 445, 555)
            storage.start_session(started, "C:\\")

            recovered_count = storage.recover_active_sessions(recovered)

            session = storage.latest_completed_session()
            self.assertEqual(recovered_count, 1)
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.status, "interrupted")
            self.assertEqual(session.change_bytes, 45)

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")

            self.assertEqual(storage.get_setting("close_behavior", "ask"), "ask")
            storage.set_setting("close_behavior", "quick")

            self.assertEqual(storage.get_setting("close_behavior", "ask"), "quick")

    def test_prune_snapshots_keeps_recent_raw_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "test.db"
            storage = Storage(database_path)
            root = str(Path(temp_dir) / "root")
            old_result = make_result(root, 10)
            old_result.started_at -= timedelta(days=100)
            old_result.finished_at -= timedelta(days=100)
            recent_result = make_result(root, 20)
            old_id = storage.save_scan(old_result)
            recent_id = storage.save_scan(recent_result)

            deleted = storage.prune_snapshots(retention_days=90)

            with closing(sqlite3.connect(database_path)) as connection:
                remaining = {
                    row[0]
                    for row in connection.execute("SELECT id FROM snapshots")
                }
                old_item_count = connection.execute(
                    "SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id = ?",
                    (old_id,),
                ).fetchone()[0]
            self.assertEqual(deleted, 1)
            self.assertNotIn(old_id, remaining)
            self.assertIn(recent_id, remaining)
            self.assertEqual(old_item_count, 0)

    def test_snapshot_changes_include_decreased_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "test.db")
            root = str(Path(temp_dir) / "root")
            first = make_result(root, 25)
            now = datetime.now()
            second = ScanResult(
                root_path=root,
                started_at=now,
                finished_at=now,
                total_bytes=0,
                file_count=0,
                directory_count=1,
                error_count=0,
                items=[
                    ScanItem(
                        root,
                        str(Path(root).parent),
                        Path(root).name,
                        "directory",
                        0,
                        0,
                        0,
                    )
                ],
            )
            first_id = storage.save_scan(first)
            second_id = storage.save_scan(second)

            decreases = storage.compare_snapshot_changes(
                second_id, first_id, direction="decrease"
            )

            deleted_file = next(
                item for item in decreases if item.name == "growing.bin"
            )
            self.assertEqual(deleted_file.new_size_bytes, 0)
            self.assertEqual(deleted_file.change_bytes, -25)


if __name__ == "__main__":
    unittest.main()
