from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from disk_monitor.readonly import ReadOnlyDatabase
from disk_monitor.storage import CURRENT_SCHEMA_VERSION, Storage, V1_BUSINESS_TABLES


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in V1_BUSINESS_TABLES
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate and verify one SQLite backup copy without writing the source."
    )
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source_path = args.source.resolve()
    if not source_path.is_file():
        parser.error(f"数据库不存在：{source_path}")
    source_before = source_path.stat()
    source_uri = f"{source_path.as_uri()}?mode=ro"
    with tempfile.TemporaryDirectory(prefix="DiskMonitorProductionCopy-") as temp:
        copy_path = Path(temp) / "monitor.db"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        try:
            source_version = int(source.execute("PRAGMA user_version").fetchone()[0])
            source_integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
            source_counts = table_counts(source)
            source_settings = dict(
                source.execute("SELECT key, value FROM app_settings").fetchall()
            )
            destination = sqlite3.connect(copy_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

        storage = Storage(copy_path)
        backup_path = storage.migration_backup_path
        migrated = sqlite3.connect(copy_path)
        try:
            migrated_version = int(
                migrated.execute("PRAGMA user_version").fetchone()[0]
            )
            migrated_integrity = migrated.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            foreign_key_errors = len(
                migrated.execute("PRAGMA foreign_key_check").fetchall()
            )
            migrated_counts = table_counts(migrated)
            migrated_settings = dict(
                migrated.execute("SELECT key, value FROM app_settings").fetchall()
            )
            directory_metric_count = int(
                migrated.execute(
                    "SELECT COUNT(*) FROM snapshot_directory_metrics"
                ).fetchone()[0]
            )
        finally:
            migrated.close()
        readonly_count = len(ReadOnlyDatabase(copy_path).list_snapshots(limit=1))
        backup_integrity = None
        if backup_path is not None:
            backup = sqlite3.connect(
                f"{backup_path.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                backup_integrity = backup.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                backup.close()

    source_after = source_path.stat()
    non_setting_counts_unchanged = all(
        source_counts[table] == migrated_counts[table]
        for table in V1_BUSINESS_TABLES
        if table != "app_settings"
    )
    original_settings_unchanged = all(
        migrated_settings.get(key) == value
        for key, value in source_settings.items()
    )
    result = {
        "source_version": source_version,
        "source_integrity": source_integrity,
        "source_size_unchanged": source_before.st_size == source_after.st_size,
        "source_mtime_unchanged": source_before.st_mtime_ns == source_after.st_mtime_ns,
        "migrated_version": migrated_version,
        "expected_version": CURRENT_SCHEMA_VERSION,
        "migrated_integrity": migrated_integrity,
        "foreign_key_error_count": foreign_key_errors,
        "business_counts_unchanged": non_setting_counts_unchanged,
        "original_settings_unchanged": original_settings_unchanged,
        "new_setting_keys": sorted(set(migrated_settings) - set(source_settings)),
        "source_counts": source_counts,
        "directory_metric_count_for_legacy_snapshots": directory_metric_count,
        "readonly_first_page_count": readonly_count,
        "migration_backup_created": backup_path is not None,
        "migration_backup_integrity": backup_integrity,
        "temporary_copy_deleted": not copy_path.exists(),
        "hashes_computed": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not all(
        (
            source_integrity == "ok",
            result["source_size_unchanged"],
            result["source_mtime_unchanged"],
            migrated_version == CURRENT_SCHEMA_VERSION,
            migrated_integrity == "ok",
            foreign_key_errors == 0,
            result["business_counts_unchanged"],
            result["original_settings_unchanged"],
            backup_integrity == "ok",
            result["temporary_copy_deleted"],
        )
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
