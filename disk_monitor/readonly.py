from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .control_protocol import ControlError
from .storage import CURRENT_SCHEMA_VERSION, default_database_path


class ReadOnlyDatabase:
    """不初始化、不迁移、不开 WAL 的历史数据查询入口。"""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or default_database_path())

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.database_path.is_file():
            raise ControlError(
                "not_found", f"监控数据库不存在：{self.database_path}"
            )
        uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as error:
            raise ControlError("gui_unavailable", "无法只读打开监控数据库") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if schema_version != CURRENT_SCHEMA_VERSION:
                raise ControlError(
                    "unsupported_schema",
                    "数据库版本不受当前 CLI 支持："
                    f"{schema_version} != {CURRENT_SCHEMA_VERSION}",
                )
            yield connection
        finally:
            connection.close()

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
    def _session_from_row(row: sqlite3.Row) -> dict[str, Any]:
        end_used = row["end_used_bytes"]
        return {
            "id": row["id"],
            "drive": row["drive"],
            "root_path": row["root_path"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "start_used_bytes": row["start_used_bytes"],
            "end_used_bytes": end_used,
            "change_bytes": (
                end_used - row["start_used_bytes"] if end_used is not None else None
            ),
            "start_snapshot_id": row["start_snapshot_id"],
            "end_snapshot_id": row["end_snapshot_id"],
            "end_reason": row["end_reason"],
            "status": row["status"],
        }

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "root_path": row["root_path"],
            "finished_at": row["finished_at"],
            "total_bytes": row["total_bytes"],
            "note": row["note"],
            "source": row["effective_source"],
        }

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def latest_disk_sample(self, drive: str | None = None) -> dict[str, Any] | None:
        parameters: tuple[object, ...] = ()
        where_clause = ""
        if drive is not None:
            where_clause = "WHERE drive = ?"
            parameters = (drive,)
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT recorded_at, drive, total_bytes, used_bytes, free_bytes
                FROM disk_samples
                {where_clause}
                ORDER BY recorded_at DESC, id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def latest_completed_session(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM monitor_sessions
                WHERE status != 'active'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self._session_from_row(row) if row else None

    def session_growth(
        self,
        session_id: int,
        *,
        direction: str = "increase",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if direction not in {"increase", "decrease"}:
            raise ControlError("invalid_args", "direction 必须是 increase 或 decrease")
        if limit < 1:
            raise ControlError("invalid_args", "limit 必须至少为 1")
        comparator = ">" if direction == "increase" else "<"
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT path, name, kind, old_size_bytes, new_size_bytes,
                       change_bytes
                FROM session_growth_items
                WHERE session_id = ? AND change_bytes {comparator} 0
                ORDER BY ABS(change_bytes) DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "parent_path": os.path.dirname(row["path"]),
                "name": row["name"],
                "kind": row["kind"],
                "old_size_bytes": row["old_size_bytes"],
                "new_size_bytes": row["new_size_bytes"],
                "change_bytes": row["change_bytes"],
            }
            for row in rows
        ]

    def list_snapshots(
        self,
        *,
        limit: int = 200,
        root_path: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ControlError("invalid_args", "limit 必须至少为 1")
        parameters: list[object] = []
        where_clause = ""
        if root_path:
            where_clause = "WHERE s.root_path = ? COLLATE NOCASE"
            parameters.append(os.path.normcase(os.path.abspath(root_path)))
        parameters.append(limit)
        with self.connection() as connection:
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
        return [self._snapshot_from_row(row) for row in rows]

    def snapshot_info(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
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
        return self._snapshot_from_row(row) if row else None

    def compare_snapshots(
        self,
        new_snapshot_id: int,
        old_snapshot_id: int,
        *,
        direction: str = "increase",
        limit: int = 100,
    ) -> dict[str, Any]:
        if direction not in {"increase", "decrease"}:
            raise ControlError("invalid_args", "direction 必须是 increase 或 decrease")
        new_snapshot = self.snapshot_info(new_snapshot_id)
        old_snapshot = self.snapshot_info(old_snapshot_id)
        if new_snapshot is None or old_snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        if os.path.normcase(new_snapshot["root_path"]) != os.path.normcase(
            old_snapshot["root_path"]
        ):
            raise ControlError("invalid_args", "只能比较相同路径的快照")
        comparator = ">" if direction == "increase" else "<"
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                WITH changes AS (
                    SELECT new.path, new.parent_path, new.name, new.kind,
                           COALESCE(old.size_bytes, 0) AS old_size_bytes,
                           new.size_bytes AS new_size_bytes
                    FROM snapshot_items AS new
                    LEFT JOIN snapshot_items AS old
                        ON old.snapshot_id = ? AND old.path = new.path
                    WHERE new.snapshot_id = ?
                    UNION ALL
                    SELECT old.path, old.parent_path, old.name, old.kind,
                           old.size_bytes AS old_size_bytes, 0 AS new_size_bytes
                    FROM snapshot_items AS old
                    LEFT JOIN snapshot_items AS new
                        ON new.snapshot_id = ? AND new.path = old.path
                    WHERE old.snapshot_id = ? AND new.path IS NULL
                )
                SELECT *, new_size_bytes - old_size_bytes AS change_bytes
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
        return {
            "new_snapshot": new_snapshot,
            "old_snapshot": old_snapshot,
            "direction": direction,
            "items": [dict(row) for row in rows],
        }

    def snapshot_tree(
        self,
        snapshot_id: int,
        path: str | None = None,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        snapshot = self.snapshot_info(snapshot_id)
        if snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        normalized_path = os.path.normcase(
            os.path.abspath(path or snapshot["root_path"])
        )
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT path, parent_path, name, kind, size_bytes,
                       file_count, depth, modified_at
                FROM snapshot_items
                WHERE snapshot_id = ? AND parent_path = ? COLLATE NOCASE
                ORDER BY size_bytes DESC, path
                LIMIT ?
                """,
                (snapshot_id, normalized_path, limit),
            ).fetchall()
        return {
            "snapshot": snapshot,
            "path": normalized_path,
            "items": [dict(row) for row in rows],
            "coverage": "数据库原始快照只保证记录深度范围和全局大文件",
        }
