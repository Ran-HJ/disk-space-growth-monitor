from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .accounting import compare_recorded_accounting
from .models import (
    DiskSample,
    GrowthItem,
    MonitorSession,
    ScanItem,
    ScanResult,
    SessionBoundary,
    SnapshotInfo,
)
from .scan_config import compare_scan_configs


SNAPSHOT_SOURCES = {
    "baseline",
    "closing",
    "manual",
    "navigation",
    "manual_save",
}

CURRENT_SCHEMA_VERSION = 3
FULL_DIRECTORY_SOURCES = {"baseline", "closing", "manual_save"}
V1_BUSINESS_TABLES = (
    "app_settings",
    "disk_samples",
    "monitor_sessions",
    "session_growth_items",
    "snapshot_items",
    "snapshots",
)

SNAPSHOT_V2_COLUMNS = {
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

SNAPSHOT_ITEM_V2_COLUMNS = {
    "allocated_size_bytes": "INTEGER",
    "unique_allocated_size_bytes": "INTEGER",
    "volume_serial_hex": "TEXT",
    "file_id": "BLOB",
    "link_count": "INTEGER",
    "is_unique_owner": "INTEGER",
    "measurement_state": "TEXT NOT NULL DEFAULT 'legacy'",
}

SNAPSHOT_V3_COLUMNS = {
    "scan_config_version": "INTEGER NOT NULL DEFAULT 0",
    "scan_config_json": "TEXT",
    "directory_summary_state": "TEXT NOT NULL DEFAULT 'legacy'",
    "excluded_rule_count": "INTEGER NOT NULL DEFAULT 0",
    "excluded_item_count": "INTEGER NOT NULL DEFAULT 0",
}


def default_database_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "DiskGrowthMonitor" / "monitor.db"


class Storage:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or default_database_path())
        self.migration_backup_path: Path | None = None
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _migration_signature(
        path: Path,
    ) -> tuple[int, tuple[int, ...], tuple[object, ...]]:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=30)
            try:
                integrity = [
                    row[0]
                    for row in connection.execute("PRAGMA integrity_check")
                ]
                if integrity != ["ok"]:
                    raise RuntimeError("数据库完整性检查失败")
                foreign_key_issues = list(
                    connection.execute("PRAGMA foreign_key_check")
                )
                if foreign_key_issues:
                    raise RuntimeError("数据库外键检查失败")
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                counts = tuple(
                    int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    )
                    for table in V1_BUSINESS_TABLES
                )
                representative_values = (
                    connection.execute(
                        "SELECT MAX(id), MAX(recorded_at) FROM disk_samples"
                    ).fetchone(),
                    connection.execute(
                        "SELECT MAX(id), MAX(finished_at) FROM snapshots"
                    ).fetchone(),
                    connection.execute(
                        "SELECT MAX(snapshot_id), MAX(path) FROM snapshot_items"
                    ).fetchone(),
                    connection.execute(
                        "SELECT MAX(id), MAX(started_at) FROM monitor_sessions"
                    ).fetchone(),
                    connection.execute(
                        "SELECT MAX(session_id), MAX(path) "
                        "FROM session_growth_items"
                    ).fetchone(),
                    connection.execute(
                        "SELECT MAX(key) FROM app_settings"
                    ).fetchone(),
                )
                return version, counts, tuple(
                    tuple(row) for row in representative_values
                )
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise RuntimeError("无法验证数据库升级备份") from error

    def _ensure_schema_backup(
        self,
        *,
        source_version: int,
        target_version: int,
    ) -> Path:
        source_signature = self._migration_signature(self.database_path)
        if source_signature[0] != source_version:
            raise RuntimeError(
                "数据库升级备份版本不匹配："
                f"{source_signature[0]} != {source_version}"
            )

        backup_directory = self.database_path.parent / "backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        backup_prefix = (
            f"{self.database_path.stem}-v{source_version}-"
            f"before-v{target_version}"
        )
        pattern = f"{backup_prefix}-*.db"
        for candidate in sorted(
            backup_directory.glob(pattern),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        ):
            try:
                if self._migration_signature(candidate) == source_signature:
                    return candidate
            except RuntimeError:
                continue

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_directory / (
            f"{backup_prefix}-{timestamp}.db"
        )
        suffix = 2
        while backup_path.exists():
            backup_path = backup_directory / (
                f"{backup_prefix}-{timestamp}-{suffix}.db"
            )
            suffix += 1
        partial_path = backup_path.with_suffix(".db.partial")
        partial_path.unlink(missing_ok=True)
        try:
            source_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            source = sqlite3.connect(source_uri, uri=True, timeout=30)
            destination = sqlite3.connect(partial_path, timeout=30)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            if self._migration_signature(partial_path) != source_signature:
                raise RuntimeError("数据库升级备份与源库记录不一致")
            partial_path.replace(backup_path)
            return backup_path
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise

    def _ensure_pre_v2_backup(self) -> Path:
        return self._ensure_schema_backup(source_version=1, target_version=2)

    def _ensure_pre_v3_backup(self) -> Path:
        return self._ensure_schema_backup(source_version=2, target_version=3)

    def _apply_schema_v2(
        self,
        connection: sqlite3.Connection,
        backup_path: Path | None,
    ) -> None:
        snapshot_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(snapshots)")
        }
        if "note" not in snapshot_columns:
            connection.execute("ALTER TABLE snapshots ADD COLUMN note TEXT")
        if "source" not in snapshot_columns:
            connection.execute("ALTER TABLE snapshots ADD COLUMN source TEXT")
        for name, definition in SNAPSHOT_V2_COLUMNS.items():
            if name not in snapshot_columns:
                connection.execute(
                    f"ALTER TABLE snapshots ADD COLUMN {name} {definition}"
                )

        item_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(snapshot_items)"
            )
        }
        for name, definition in SNAPSHOT_ITEM_V2_COLUMNS.items():
            if name not in item_columns:
                connection.execute(
                    "ALTER TABLE snapshot_items "
                    f"ADD COLUMN {name} {definition}"
                )
        if backup_path is not None:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("schema_v2_backup_path", str(backup_path)),
            )

    def _apply_schema_v3(
        self,
        connection: sqlite3.Connection,
        backup_path: Path | None,
    ) -> None:
        snapshot_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(snapshots)")
        }
        for name, definition in SNAPSHOT_V3_COLUMNS.items():
            if name not in snapshot_columns:
                connection.execute(
                    f"ALTER TABLE snapshots ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS directory_paths (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL COLLATE NOCASE UNIQUE,
                parent_id INTEGER REFERENCES directory_paths(id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_directory_paths_parent
                ON directory_paths(parent_id, path)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_directory_metrics (
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
                    ON DELETE CASCADE,
                path_id INTEGER NOT NULL REFERENCES directory_paths(id),
                total_bytes INTEGER NOT NULL,
                allocated_size_bytes INTEGER,
                unique_allocated_size_bytes INTEGER,
                measured_allocated_bytes INTEGER NOT NULL DEFAULT 0,
                measured_unique_allocated_bytes INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL,
                directory_count INTEGER NOT NULL,
                direct_file_bytes INTEGER NOT NULL,
                direct_file_count INTEGER NOT NULL,
                eligible_file_count INTEGER NOT NULL DEFAULT 0,
                allocation_measured_file_count INTEGER NOT NULL DEFAULT 0,
                identity_measured_file_count INTEGER NOT NULL DEFAULT 0,
                metadata_error_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                measurement_state TEXT NOT NULL,
                PRIMARY KEY(snapshot_id, path_id)
            ) WITHOUT ROWID
            """
        )
        if backup_path is not None:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("schema_v3_backup_path", str(backup_path)),
            )

    def _initialize(self) -> None:
        probe = sqlite3.connect(self.database_path, timeout=30)
        try:
            schema_version = int(
                probe.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            probe.close()
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "数据库版本高于当前程序支持的版本："
                f"{schema_version} > {CURRENT_SCHEMA_VERSION}"
            )

        if schema_version == 1:
            self.migration_backup_path = self._ensure_pre_v2_backup()
        elif schema_version == 2:
            self.migration_backup_path = self._ensure_pre_v3_backup()

        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS disk_samples (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    drive TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL,
                    used_bytes INTEGER NOT NULL,
                    free_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_drive_time
                    ON disk_samples(drive, recorded_at);

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    directory_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    note TEXT,
                    source TEXT,
                    allocated_total_bytes INTEGER,
                    unique_allocated_total_bytes INTEGER,
                    measured_allocated_bytes INTEGER NOT NULL DEFAULT 0,
                    measured_unique_allocated_bytes INTEGER NOT NULL DEFAULT 0,
                    eligible_file_count INTEGER NOT NULL DEFAULT 0,
                    allocation_measured_file_count INTEGER NOT NULL DEFAULT 0,
                    identity_measured_file_count INTEGER NOT NULL DEFAULT 0,
                    metadata_error_count INTEGER NOT NULL DEFAULT 0,
                    measurement_state TEXT NOT NULL DEFAULT 'legacy'
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_root_time
                    ON snapshots(root_path, finished_at);

                CREATE TABLE IF NOT EXISTS snapshot_items (
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
                    allocated_size_bytes INTEGER,
                    unique_allocated_size_bytes INTEGER,
                    volume_serial_hex TEXT,
                    file_id BLOB,
                    link_count INTEGER,
                    is_unique_owner INTEGER,
                    measurement_state TEXT NOT NULL DEFAULT 'legacy',
                    PRIMARY KEY(snapshot_id, path)
                );
                CREATE INDEX IF NOT EXISTS idx_items_snapshot_parent
                    ON snapshot_items(snapshot_id, parent_path);

                CREATE TABLE IF NOT EXISTS monitor_sessions (
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
                CREATE INDEX IF NOT EXISTS idx_sessions_status_time
                    ON monitor_sessions(status, started_at);

                CREATE TABLE IF NOT EXISTS session_growth_items (
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
                CREATE INDEX IF NOT EXISTS idx_session_growth_change
                    ON session_growth_items(session_id, change_bytes DESC);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._apply_schema_v2(
                    connection,
                    self.migration_backup_path if schema_version == 1 else None,
                )
                self._apply_schema_v3(
                    connection, self.migration_backup_path
                )
                connection.execute(
                    f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def add_disk_sample(self, sample: DiskSample) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO disk_samples(
                    recorded_at, drive, total_bytes, used_bytes, free_bytes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sample.recorded_at.isoformat(timespec="seconds"),
                    sample.drive,
                    sample.total_bytes,
                    sample.used_bytes,
                    sample.free_bytes,
                ),
            )

    def get_disk_samples(self, drive: str, hours: int = 24) -> list[DiskSample]:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(
            timespec="seconds"
        )
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT recorded_at, drive, total_bytes, used_bytes, free_bytes
                FROM disk_samples
                WHERE drive = ? AND recorded_at >= ?
                ORDER BY recorded_at
                """,
                (drive, cutoff),
            ).fetchall()
        return [
            DiskSample(
                recorded_at=datetime.fromisoformat(row["recorded_at"]),
                drive=row["drive"],
                total_bytes=row["total_bytes"],
                used_bytes=row["used_bytes"],
                free_bytes=row["free_bytes"],
            )
            for row in rows
        ]

    def get_session_boundaries(
        self, drive: str, hours: int = 24
    ) -> list[SessionBoundary]:
        cutoff = datetime.now() - timedelta(hours=hours)
        cutoff_text = cutoff.isoformat(timespec="seconds")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT started_at, ended_at
                FROM monitor_sessions
                WHERE drive = ?
                  AND (started_at >= ? OR ended_at >= ?)
                ORDER BY started_at
                """,
                (drive, cutoff_text, cutoff_text),
            ).fetchall()
        boundaries: list[SessionBoundary] = []
        for row in rows:
            started_at = datetime.fromisoformat(row["started_at"])
            if started_at >= cutoff:
                boundaries.append(SessionBoundary(started_at, "start"))
            if row["ended_at"]:
                ended_at = datetime.fromisoformat(row["ended_at"])
                if ended_at >= cutoff:
                    boundaries.append(SessionBoundary(ended_at, "end"))
        return sorted(boundaries, key=lambda item: item.occurred_at)

    def get_latest_disk_sample(self, drive: str) -> DiskSample | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT recorded_at, drive, total_bytes, used_bytes, free_bytes
                FROM disk_samples
                WHERE drive = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT 1
                """,
                (drive,),
            ).fetchone()
        if row is None:
            return None
        return DiskSample(
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            drive=row["drive"],
            total_bytes=row["total_bytes"],
            used_bytes=row["used_bytes"],
            free_bytes=row["free_bytes"],
        )

    def prune_disk_samples(
        self, retention_days: int = 30, *, now: datetime | None = None
    ) -> int:
        if retention_days < 1:
            raise ValueError("retention_days 必须至少为 1")
        cutoff = ((now or datetime.now()) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM disk_samples WHERE recorded_at < ?", (cutoff,)
            )
            return cursor.rowcount

    def prune_snapshots(
        self, retention_days: int = 90, *, now: datetime | None = None
    ) -> int:
        if retention_days < 1:
            raise ValueError("retention_days 必须至少为 1")
        cutoff = ((now or datetime.now()) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM snapshots
                WHERE finished_at < ?
                    AND id NOT IN (
                        SELECT start_snapshot_id FROM monitor_sessions
                        WHERE status = 'active' AND start_snapshot_id IS NOT NULL
                    )
                    AND id NOT IN (
                        SELECT end_snapshot_id FROM monitor_sessions
                        WHERE status = 'active' AND end_snapshot_id IS NOT NULL
                    )
                """,
                (cutoff,),
            )
            return cursor.rowcount

    def prune_directory_metrics(self, *, now: datetime | None = None) -> int:
        reference_time = now or datetime.now()
        baseline_cutoff = (reference_time - timedelta(days=7)).isoformat(
            timespec="seconds"
        )
        closing_cutoff = (reference_time - timedelta(days=30)).isoformat(
            timespec="seconds"
        )
        manual_cutoff = (reference_time - timedelta(days=90)).isoformat(
            timespec="seconds"
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM snapshot_directory_metrics
                WHERE snapshot_id IN (
                    SELECT s.id
                    FROM snapshots AS s
                    WHERE (
                        (s.source = 'baseline' AND s.finished_at < ?)
                        OR (s.source = 'closing' AND s.finished_at < ?)
                        OR (s.source = 'manual_save' AND s.finished_at < ?)
                    )
                    AND s.id NOT IN (
                        SELECT start_snapshot_id FROM monitor_sessions
                        WHERE status = 'active'
                          AND start_snapshot_id IS NOT NULL
                    )
                    AND s.id NOT IN (
                        SELECT end_snapshot_id FROM monitor_sessions
                        WHERE status = 'active'
                          AND end_snapshot_id IS NOT NULL
                    )
                )
                """,
                (baseline_cutoff, closing_cutoff, manual_cutoff),
            )
            connection.execute(
                """
                UPDATE snapshots
                SET directory_summary_state = 'expired'
                WHERE directory_summary_state = 'complete'
                  AND NOT EXISTS (
                      SELECT 1 FROM snapshot_directory_metrics AS metrics
                      WHERE metrics.snapshot_id = snapshots.id
                  )
                """
            )
            return cursor.rowcount

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    @staticmethod
    def _directory_summary_state(result: ScanResult, source: str | None) -> str:
        if source not in FULL_DIRECTORY_SOURCES:
            return "not_saved"
        if (
            result.skeleton is None
            or len(result.skeleton.nodes) != result.directory_count
        ):
            return "unavailable"
        return "complete"

    @staticmethod
    def _save_directory_metrics(
        connection: sqlite3.Connection,
        snapshot_id: int,
        result: ScanResult,
    ) -> None:
        skeleton = result.skeleton
        if skeleton is None:
            raise ValueError("目录骨架不可用")
        connection.execute("DROP TABLE IF EXISTS temp.pending_directory_metrics")
        connection.execute(
            """
            CREATE TEMP TABLE pending_directory_metrics (
                path TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
                parent_path TEXT COLLATE NOCASE,
                total_bytes INTEGER NOT NULL,
                allocated_size_bytes INTEGER,
                unique_allocated_size_bytes INTEGER,
                measured_allocated_bytes INTEGER NOT NULL,
                measured_unique_allocated_bytes INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                directory_count INTEGER NOT NULL,
                direct_file_bytes INTEGER NOT NULL,
                direct_file_count INTEGER NOT NULL,
                eligible_file_count INTEGER NOT NULL,
                allocation_measured_file_count INTEGER NOT NULL,
                identity_measured_file_count INTEGER NOT NULL,
                metadata_error_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                measurement_state TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.executemany(
            """
            INSERT INTO pending_directory_metrics(
                path, parent_path, total_bytes, allocated_size_bytes,
                unique_allocated_size_bytes, measured_allocated_bytes,
                measured_unique_allocated_bytes, file_count,
                directory_count, direct_file_bytes, direct_file_count,
                eligible_file_count, allocation_measured_file_count,
                identity_measured_file_count, metadata_error_count,
                error_count, modified_at, measurement_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    path,
                    None if path == result.root_path else os.path.dirname(path),
                    node.total_bytes,
                    node.allocated_size_bytes,
                    node.unique_allocated_size_bytes,
                    node.measured_allocated_bytes,
                    node.measured_unique_allocated_bytes,
                    node.file_count,
                    node.directory_count,
                    node.direct_file_bytes,
                    node.direct_file_count,
                    node.eligible_file_count,
                    node.allocation_measured_file_count,
                    node.identity_measured_file_count,
                    node.metadata_error_count,
                    node.error_count,
                    node.modified_at,
                    node.measurement_state,
                )
                for path, node in skeleton.nodes.items()
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO directory_paths(path)
            SELECT path FROM pending_directory_metrics
            """
        )
        connection.execute(
            """
            UPDATE directory_paths
            SET parent_id = (
                SELECT parent.id
                FROM pending_directory_metrics AS pending
                LEFT JOIN directory_paths AS parent
                    ON parent.path = pending.parent_path COLLATE NOCASE
                WHERE pending.path = directory_paths.path COLLATE NOCASE
            )
            WHERE path IN (SELECT path FROM pending_directory_metrics)
            """
        )
        connection.execute(
            """
            INSERT INTO snapshot_directory_metrics(
                snapshot_id, path_id, total_bytes,
                allocated_size_bytes, unique_allocated_size_bytes,
                measured_allocated_bytes, measured_unique_allocated_bytes,
                file_count, directory_count, direct_file_bytes,
                direct_file_count, eligible_file_count,
                allocation_measured_file_count,
                identity_measured_file_count, metadata_error_count,
                error_count, modified_at, measurement_state
            )
            SELECT ?, paths.id, pending.total_bytes,
                   pending.allocated_size_bytes,
                   pending.unique_allocated_size_bytes,
                   pending.measured_allocated_bytes,
                   pending.measured_unique_allocated_bytes,
                   pending.file_count, pending.directory_count,
                   pending.direct_file_bytes, pending.direct_file_count,
                   pending.eligible_file_count,
                   pending.allocation_measured_file_count,
                   pending.identity_measured_file_count,
                   pending.metadata_error_count, pending.error_count,
                   pending.modified_at, pending.measurement_state
            FROM pending_directory_metrics AS pending
            JOIN directory_paths AS paths
                ON paths.path = pending.path COLLATE NOCASE
            """,
            (snapshot_id,),
        )
        connection.execute("DROP TABLE pending_directory_metrics")

    def save_scan(
        self,
        result: ScanResult,
        *,
        note: str | None = None,
        source: str | None = None,
    ) -> int:
        if source is not None and source not in SNAPSHOT_SOURCES:
            raise ValueError(f"未知快照来源：{source}")
        directory_summary_state = self._directory_summary_state(result, source)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots(
                    root_path, started_at, finished_at, total_bytes,
                    file_count, directory_count, error_count, note, source,
                    allocated_total_bytes, unique_allocated_total_bytes,
                    measured_allocated_bytes, measured_unique_allocated_bytes,
                    eligible_file_count, allocation_measured_file_count,
                    identity_measured_file_count, metadata_error_count,
                    measurement_state, scan_config_version, scan_config_json,
                    directory_summary_state, excluded_rule_count,
                    excluded_item_count
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    result.root_path,
                    result.started_at.isoformat(timespec="seconds"),
                    result.finished_at.isoformat(timespec="seconds"),
                    result.total_bytes,
                    result.file_count,
                    result.directory_count,
                    result.error_count,
                    note.strip() if note and note.strip() else None,
                    source,
                    result.allocated_total_bytes,
                    result.unique_allocated_total_bytes,
                    result.measured_allocated_bytes,
                    result.measured_unique_allocated_bytes,
                    result.eligible_file_count,
                    result.allocation_measured_file_count,
                    result.identity_measured_file_count,
                    result.metadata_error_count,
                    result.measurement_state,
                    result.scan_config_version,
                    result.scan_config_json,
                    directory_summary_state,
                    result.excluded_rule_count,
                    result.excluded_item_count,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO snapshot_items(
                    snapshot_id, path, parent_path, name, kind, size_bytes,
                    file_count, depth, modified_at, allocated_size_bytes,
                    unique_allocated_size_bytes, volume_serial_hex, file_id,
                    link_count, is_unique_owner, measurement_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item.path,
                        item.parent_path,
                        item.name,
                        item.kind,
                        item.size_bytes,
                        item.file_count,
                        item.depth,
                        item.modified_at,
                        item.allocated_size_bytes,
                        item.unique_allocated_size_bytes,
                        item.volume_serial_hex,
                        item.file_id,
                        item.link_count,
                        (
                            int(item.is_unique_owner)
                            if item.is_unique_owner is not None
                            else None
                        ),
                        item.measurement_state,
                    )
                    for item in result.items
                ],
            )
            if directory_summary_state == "complete":
                self._save_directory_metrics(connection, snapshot_id, result)
        result.snapshot_id = snapshot_id
        return snapshot_id

    def load_snapshot(self, snapshot_id: int) -> ScanResult | None:
        with self._connection() as connection:
            snapshot = connection.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                return None
            rows = connection.execute(
                """
                SELECT path, parent_path, name, kind, size_bytes, file_count,
                       depth, modified_at, allocated_size_bytes,
                       unique_allocated_size_bytes, volume_serial_hex, file_id,
                       link_count, is_unique_owner, measurement_state
                FROM snapshot_items
                WHERE snapshot_id = ?
                ORDER BY path
                """,
                (snapshot_id,),
            ).fetchall()
        return ScanResult(
            root_path=snapshot["root_path"],
            started_at=datetime.fromisoformat(snapshot["started_at"]),
            finished_at=datetime.fromisoformat(snapshot["finished_at"]),
            total_bytes=snapshot["total_bytes"],
            file_count=snapshot["file_count"],
            directory_count=snapshot["directory_count"],
            error_count=snapshot["error_count"],
            items=[
                ScanItem(
                    path=row["path"],
                    parent_path=row["parent_path"],
                    name=row["name"],
                    kind=row["kind"],
                    size_bytes=row["size_bytes"],
                    file_count=row["file_count"],
                    depth=row["depth"],
                    modified_at=row["modified_at"],
                    allocated_size_bytes=row["allocated_size_bytes"],
                    unique_allocated_size_bytes=(
                        row["unique_allocated_size_bytes"]
                    ),
                    volume_serial_hex=row["volume_serial_hex"],
                    file_id=row["file_id"],
                    link_count=row["link_count"],
                    is_unique_owner=(
                        bool(row["is_unique_owner"])
                        if row["is_unique_owner"] is not None
                        else None
                    ),
                    measurement_state=row["measurement_state"],
                )
                for row in rows
            ],
            snapshot_id=snapshot_id,
            allocated_total_bytes=snapshot["allocated_total_bytes"],
            unique_allocated_total_bytes=(
                snapshot["unique_allocated_total_bytes"]
            ),
            measured_allocated_bytes=snapshot["measured_allocated_bytes"],
            measured_unique_allocated_bytes=(
                snapshot["measured_unique_allocated_bytes"]
            ),
            eligible_file_count=snapshot["eligible_file_count"],
            allocation_measured_file_count=(
                snapshot["allocation_measured_file_count"]
            ),
            identity_measured_file_count=(
                snapshot["identity_measured_file_count"]
            ),
            metadata_error_count=snapshot["metadata_error_count"],
            measurement_state=snapshot["measurement_state"],
            scan_config_version=snapshot["scan_config_version"],
            scan_config_json=snapshot["scan_config_json"],
            excluded_rule_count=snapshot["excluded_rule_count"],
            excluded_item_count=snapshot["excluded_item_count"],
        )

    def latest_snapshot_id(self, root_path: str) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM snapshots
                WHERE root_path = ?
                ORDER BY id DESC LIMIT 1
                """,
                (root_path,),
            ).fetchone()
        return int(row["id"]) if row else None

    def list_marked_snapshots(
        self,
        root_path: str,
        *,
        before_snapshot_id: int | None = None,
    ) -> list[SnapshotInfo]:
        parameters: list[object] = [root_path]
        before_clause = ""
        if before_snapshot_id is not None:
            before_clause = "AND id < ?"
            parameters.append(before_snapshot_id)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, root_path, finished_at, total_bytes, note, source
                FROM snapshots
                WHERE root_path = ? AND note IS NOT NULL AND note != ''
                    {before_clause}
                ORDER BY id DESC
                """,
                parameters,
            ).fetchall()
        return [
            SnapshotInfo(
                id=row["id"],
                root_path=row["root_path"],
                finished_at=datetime.fromisoformat(row["finished_at"]),
                total_bytes=row["total_bytes"],
                note=row["note"],
                source=row["source"] or "manual_save",
            )
            for row in rows
        ]

    def get_snapshot_info(self, snapshot_id: int) -> SnapshotInfo | None:
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT s.id, s.root_path, s.finished_at, s.total_bytes,
                       COALESCE(s.note, '') AS note,
                       {self._snapshot_source_sql()} AS effective_source
                FROM snapshots AS s
                WHERE s.id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_info_from_row(row) if row else None

    def latest_full_snapshot_for_path(
        self, root_path: str
    ) -> SnapshotInfo | None:
        normalized_root = os.path.normcase(os.path.abspath(root_path))
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.id, s.root_path, s.finished_at, s.total_bytes,
                       COALESCE(s.note, '') AS note,
                       'closing' AS effective_source
                FROM monitor_sessions AS ms
                JOIN snapshots AS s ON s.id = ms.end_snapshot_id
                WHERE ms.status = 'completed'
                  AND ms.end_reason = 'normal_close'
                  AND s.root_path = ? COLLATE NOCASE
                ORDER BY s.finished_at DESC, s.id DESC
                LIMIT 1
                """,
                (normalized_root,),
            ).fetchone()
        return self._snapshot_info_from_row(row) if row else None

    def list_snapshots(
        self,
        *,
        limit: int = 200,
        cursor: tuple[datetime, int] | None = None,
        root_path: str | None = None,
        source: str | None = None,
        finished_after: datetime | None = None,
        finished_before: datetime | None = None,
    ) -> list[SnapshotInfo]:
        if limit < 1:
            raise ValueError("limit 必须至少为 1")
        if source is not None and source not in SNAPSHOT_SOURCES:
            raise ValueError(f"未知快照来源：{source}")
        if (
            finished_after is not None
            and finished_before is not None
            and finished_after > finished_before
        ):
            raise ValueError("开始时间不能晚于结束时间")
        parameters: list[object] = []
        conditions: list[str] = []
        if cursor is not None:
            cursor_time = cursor[0].isoformat(timespec="seconds")
            conditions.append(
                "(s.finished_at < ? OR (s.finished_at = ? AND s.id < ?))"
            )
            parameters.extend((cursor_time, cursor_time, cursor[1]))
        if root_path:
            conditions.append("s.root_path = ? COLLATE NOCASE")
            parameters.append(os.path.normcase(os.path.abspath(root_path)))
        if source is not None:
            conditions.append(f"({self._snapshot_source_sql()}) = ?")
            parameters.append(source)
        if finished_after is not None:
            conditions.append("s.finished_at >= ?")
            parameters.append(finished_after.isoformat(timespec="seconds"))
        if finished_before is not None:
            conditions.append("s.finished_at <= ?")
            parameters.append(finished_before.isoformat(timespec="seconds"))
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.root_path, s.finished_at, s.total_bytes,
                       COALESCE(s.note, '') AS note,
                       {self._snapshot_source_sql()} AS effective_source
                FROM snapshots AS s
                {where_clause}
                ORDER BY s.finished_at DESC, s.id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._snapshot_info_from_row(row) for row in rows]

    @staticmethod
    def _snapshot_source_sql() -> str:
        return """
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM monitor_sessions AS ms
                    WHERE ms.end_snapshot_id = s.id
                ) THEN 'closing'
                WHEN EXISTS (
                    SELECT 1 FROM monitor_sessions AS ms
                    WHERE ms.start_snapshot_id = s.id
                ) THEN 'baseline'
                WHEN s.source = 'manual_save'
                     OR (s.note IS NOT NULL AND s.note != '')
                    THEN 'manual_save'
                ELSE COALESCE(s.source, 'manual')
            END
        """

    @staticmethod
    def _snapshot_info_from_row(row: sqlite3.Row) -> SnapshotInfo:
        return SnapshotInfo(
            id=row["id"],
            root_path=row["root_path"],
            finished_at=datetime.fromisoformat(row["finished_at"]),
            total_bytes=row["total_bytes"],
            note=row["note"],
            source=row["effective_source"],
        )

    def previous_snapshot_id(self, result: ScanResult) -> int | None:
        if result.snapshot_id is None:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM snapshots
                WHERE root_path = ? AND id < ?
                  AND (
                      (? <= 0 AND scan_config_version <= 0)
                      OR (
                          scan_config_version = ?
                          AND scan_config_json = ?
                      )
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (
                    result.root_path,
                    result.snapshot_id,
                    result.scan_config_version,
                    result.scan_config_version,
                    result.scan_config_json,
                ),
            ).fetchone()
        return int(row["id"]) if row else None

    def snapshot_config_comparison(
        self, new_snapshot_id: int, old_snapshot_id: int
    ) -> dict[str, object]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, root_path, scan_config_version, scan_config_json
                FROM snapshots WHERE id IN (?, ?)
                """,
                (new_snapshot_id, old_snapshot_id),
            ).fetchall()
        snapshots = {int(row["id"]): row for row in rows}
        if new_snapshot_id not in snapshots or old_snapshot_id not in snapshots:
            raise ValueError("指定的快照不存在")
        new_snapshot = snapshots[new_snapshot_id]
        old_snapshot = snapshots[old_snapshot_id]
        if os.path.normcase(new_snapshot["root_path"]) != os.path.normcase(
            old_snapshot["root_path"]
        ):
            raise ValueError("只能比较相同路径的快照")
        return compare_scan_configs(
            new_snapshot["scan_config_version"],
            new_snapshot["scan_config_json"],
            old_snapshot["scan_config_version"],
            old_snapshot["scan_config_json"],
        )

    def compare_snapshots(
        self, new_snapshot_id: int, old_snapshot_id: int, limit: int = 100
    ) -> list[GrowthItem]:
        return self.compare_snapshot_changes(
            new_snapshot_id,
            old_snapshot_id,
            direction="increase",
            limit=limit,
        )

    def compare_snapshot_accounting(
        self,
        new_snapshot_id: int,
        old_snapshot_id: int,
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with self._connection() as connection:
            snapshots = {
                int(row["id"]): row
                for row in connection.execute(
                    """
                    SELECT id, root_path, total_bytes,
                           allocated_total_bytes,
                           unique_allocated_total_bytes,
                           measurement_state
                    FROM snapshots WHERE id IN (?, ?)
                    """,
                    (new_snapshot_id, old_snapshot_id),
                )
            }
            if (
                new_snapshot_id not in snapshots
                or old_snapshot_id not in snapshots
            ):
                raise ValueError("指定的快照不存在")
            rows = {
                new_snapshot_id: connection.execute(
                    """
                    SELECT path, kind, allocated_size_bytes,
                           volume_serial_hex, file_id, link_count,
                           is_unique_owner, measurement_state
                    FROM snapshot_items WHERE snapshot_id = ?
                    """,
                    (new_snapshot_id,),
                ).fetchall(),
                old_snapshot_id: connection.execute(
                    """
                    SELECT path, kind, allocated_size_bytes,
                           volume_serial_hex, file_id, link_count,
                           is_unique_owner, measurement_state
                    FROM snapshot_items WHERE snapshot_id = ?
                    """,
                    (old_snapshot_id,),
                ).fetchall(),
            }
        result = compare_recorded_accounting(
            snapshots[new_snapshot_id],
            snapshots[old_snapshot_id],
            rows[new_snapshot_id],
            rows[old_snapshot_id],
        )
        result["item_count"] = len(result["items"])
        result["unresolved_item_count"] = len(result["unresolved_items"])
        result["items"] = result["items"][:limit]
        result["unresolved_items"] = result["unresolved_items"][:limit]
        return result

    def compare_snapshot_changes(
        self,
        new_snapshot_id: int,
        old_snapshot_id: int,
        *,
        direction: str,
        limit: int = 100,
    ) -> list[GrowthItem]:
        if direction not in {"increase", "decrease"}:
            raise ValueError("direction 必须是 increase 或 decrease")
        config_comparison = self.snapshot_config_comparison(
            new_snapshot_id, old_snapshot_id
        )
        if config_comparison["status"] == "mismatch":
            differences = "、".join(config_comparison["differences"])
            raise ValueError(f"扫描配置不同，不能生成增长归因：{differences}")
        comparator = ">" if direction == "increase" else "<"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH changes AS (
                    SELECT
                        new.path, new.parent_path, new.name, new.kind,
                        COALESCE(old.size_bytes, 0) AS old_size_bytes,
                        new.size_bytes AS new_size_bytes
                    FROM snapshot_items AS new
                    LEFT JOIN snapshot_items AS old
                        ON old.snapshot_id = ? AND old.path = new.path
                    WHERE new.snapshot_id = ?

                    UNION ALL

                    SELECT
                        old.path, old.parent_path, old.name, old.kind,
                        old.size_bytes AS old_size_bytes,
                        0 AS new_size_bytes
                    FROM snapshot_items AS old
                    LEFT JOIN snapshot_items AS new
                        ON new.snapshot_id = ? AND new.path = old.path
                    WHERE old.snapshot_id = ? AND new.path IS NULL
                )
                SELECT path, parent_path, name, kind, old_size_bytes, new_size_bytes
                FROM changes
                WHERE new_size_bytes {comparator} old_size_bytes
                ORDER BY ABS(new_size_bytes - old_size_bytes) DESC
                LIMIT ?
                """,
                (
                    old_snapshot_id,
                    new_snapshot_id,
                    new_snapshot_id,
                    old_snapshot_id,
                    limit,
                ),
            ).fetchall()
        return [
            GrowthItem(
                path=row["path"],
                parent_path=row["parent_path"],
                name=row["name"],
                kind=row["kind"],
                old_size_bytes=row["old_size_bytes"],
                new_size_bytes=row["new_size_bytes"],
            )
            for row in rows
        ]

    def start_session(self, sample: DiskSample, root_path: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO monitor_sessions(
                    drive, root_path, started_at, start_used_bytes, status
                ) VALUES (?, ?, ?, ?, 'active')
                """,
                (
                    sample.drive,
                    root_path,
                    sample.recorded_at.isoformat(timespec="seconds"),
                    sample.used_bytes,
                ),
            )
            return int(cursor.lastrowid)

    def set_session_start_snapshot(self, session_id: int, snapshot_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE monitor_sessions
                SET start_snapshot_id = ?
                WHERE id = ? AND status = 'active'
                """,
                (snapshot_id, session_id),
            )

    def finish_session(
        self,
        session_id: int,
        sample: DiskSample,
        *,
        end_snapshot_id: int | None = None,
        end_reason: str,
    ) -> list[GrowthItem]:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE monitor_sessions
                SET ended_at = ?, end_used_bytes = ?, end_snapshot_id = ?,
                    end_reason = ?, status = 'completed'
                WHERE id = ? AND status = 'active'
                """,
                (
                    sample.recorded_at.isoformat(timespec="seconds"),
                    sample.used_bytes,
                    end_snapshot_id,
                    end_reason,
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM monitor_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise ValueError(f"监控会话不存在：{session_id}")
            else:
                session = connection.execute(
                    """
                    SELECT start_snapshot_id
                    FROM monitor_sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                start_snapshot_id = session["start_snapshot_id"]
                if start_snapshot_id is not None and end_snapshot_id is not None:
                    connection.execute(
                        "DELETE FROM session_growth_items WHERE session_id = ?",
                        (session_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_growth_items(
                            session_id, path, name, kind, old_size_bytes,
                            new_size_bytes, change_bytes
                        )
                        SELECT
                            ?, new.path, new.name, new.kind,
                            COALESCE(old.size_bytes, 0), new.size_bytes,
                            new.size_bytes - COALESCE(old.size_bytes, 0)
                        FROM snapshot_items AS new
                        LEFT JOIN snapshot_items AS old
                            ON old.snapshot_id = ? AND old.path = new.path
                        WHERE new.snapshot_id = ?
                            AND new.size_bytes != COALESCE(old.size_bytes, 0)

                        UNION ALL

                        SELECT
                            ?, old.path, old.name, old.kind,
                            old.size_bytes, 0, -old.size_bytes
                        FROM snapshot_items AS old
                        LEFT JOIN snapshot_items AS new
                            ON new.snapshot_id = ? AND new.path = old.path
                        WHERE old.snapshot_id = ? AND new.path IS NULL
                        """,
                        (
                            session_id,
                            start_snapshot_id,
                            end_snapshot_id,
                            session_id,
                            end_snapshot_id,
                            start_snapshot_id,
                        ),
                    )
            return self._get_session_growth(connection, session_id, 100, "increase")

    def recover_active_sessions(self, sample: DiskSample) -> int:
        """在下次启动时为未正常结束的会话补记最终磁盘用量。"""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE monitor_sessions
                SET ended_at = ?, end_used_bytes = ?,
                    end_reason = 'recovered_after_interruption',
                    status = 'interrupted'
                WHERE status = 'active' AND drive = ?
                """,
                (
                    sample.recorded_at.isoformat(timespec="seconds"),
                    sample.used_bytes,
                    sample.drive,
                ),
            )
            return cursor.rowcount

    def get_session_growth(
        self,
        session_id: int,
        limit: int = 100,
        *,
        direction: str = "increase",
    ) -> list[GrowthItem]:
        with self._connection() as connection:
            return self._get_session_growth(
                connection, session_id, limit, direction
            )

    @staticmethod
    def _get_session_growth(
        connection: sqlite3.Connection,
        session_id: int,
        limit: int,
        direction: str,
    ) -> list[GrowthItem]:
        if direction not in {"increase", "decrease"}:
            raise ValueError("direction 必须是 increase 或 decrease")
        comparator = ">" if direction == "increase" else "<"
        rows = connection.execute(
            f"""
            SELECT path, name, kind, old_size_bytes, new_size_bytes
            FROM session_growth_items
            WHERE session_id = ? AND change_bytes {comparator} 0
            ORDER BY ABS(change_bytes) DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [
            GrowthItem(
                path=row["path"],
                parent_path=os.path.dirname(row["path"]),
                name=row["name"],
                kind=row["kind"],
                old_size_bytes=row["old_size_bytes"],
                new_size_bytes=row["new_size_bytes"],
            )
            for row in rows
        ]

    def latest_completed_session(self) -> MonitorSession | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM monitor_sessions
                WHERE status != 'active'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return MonitorSession(
            id=row["id"],
            drive=row["drive"],
            root_path=row["root_path"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=(
                datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
            ),
            start_used_bytes=row["start_used_bytes"],
            end_used_bytes=row["end_used_bytes"],
            start_snapshot_id=row["start_snapshot_id"],
            end_snapshot_id=row["end_snapshot_id"],
            end_reason=row["end_reason"],
            status=row["status"],
        )
