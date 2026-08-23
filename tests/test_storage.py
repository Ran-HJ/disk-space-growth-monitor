from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from disk_monitor.models import DiskSample, ScanItem, ScanResult
from disk_monitor.storage import Storage


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


class StorageTests(unittest.TestCase):
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
            self.assertIn("note", columns)
            self.assertIn("source", columns)

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
