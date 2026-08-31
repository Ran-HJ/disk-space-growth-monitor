from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .accounting import compare_recorded_accounting
from .control_protocol import ControlError
from .migration_advice import build_migration_advice
from .scan_config import compare_scan_configs
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
            "allocated_total_bytes": row["allocated_total_bytes"],
            "unique_allocated_total_bytes": (
                row["unique_allocated_total_bytes"]
            ),
            "measured_allocated_bytes": row["measured_allocated_bytes"],
            "measured_unique_allocated_bytes": (
                row["measured_unique_allocated_bytes"]
            ),
            "eligible_file_count": row["eligible_file_count"],
            "allocation_measured_file_count": (
                row["allocation_measured_file_count"]
            ),
            "identity_measured_file_count": (
                row["identity_measured_file_count"]
            ),
            "metadata_error_count": row["metadata_error_count"],
            "measurement_state": row["measurement_state"],
            "scan_config_version": row["scan_config_version"],
            "scan_config_json": row["scan_config_json"],
            "directory_summary_state": row["directory_summary_state"],
            "excluded_rule_count": row["excluded_rule_count"],
            "excluded_item_count": row["excluded_item_count"],
        }

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ControlError("invalid_args", "limit 必须是正整数")

    @staticmethod
    def _validate_cursor(cursor: int) -> None:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ControlError("invalid_args", "cursor 必须是非负整数")

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

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
        self._validate_limit(limit)
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
        source: str | None = None,
        finished_after: str | None = None,
        finished_before: str | None = None,
        cursor: int = 0,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        self._validate_cursor(cursor)
        if source is not None and source not in {
            "baseline",
            "closing",
            "manual_save",
            "manual",
            "navigation",
        }:
            raise ControlError("invalid_args", "未知快照来源")
        normalized_times: dict[str, str | None] = {
            "finished_after": finished_after,
            "finished_before": finished_before,
        }
        for name, value in normalized_times.items():
            if value is None:
                continue
            try:
                normalized_times[name] = datetime.fromisoformat(value).isoformat(
                    timespec="seconds"
                )
            except (TypeError, ValueError) as error:
                raise ControlError(
                    "invalid_args", f"{name} 必须是 ISO 日期时间"
                ) from error
        finished_after = normalized_times["finished_after"]
        finished_before = normalized_times["finished_before"]
        if (
            finished_after is not None
            and finished_before is not None
            and finished_after > finished_before
        ):
            raise ControlError("invalid_args", "开始时间不能晚于结束时间")
        parameters: list[object] = []
        conditions: list[str] = []
        if root_path:
            conditions.append("s.root_path = ? COLLATE NOCASE")
            parameters.append(os.path.normcase(os.path.abspath(root_path)))
        if source is not None:
            conditions.append(f"({self._snapshot_source_sql()}) = ?")
            parameters.append(source)
        if finished_after is not None:
            conditions.append("s.finished_at >= ?")
            parameters.append(finished_after)
        if finished_before is not None:
            conditions.append("s.finished_at <= ?")
            parameters.append(finished_before)
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.extend((limit, cursor))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.root_path, s.finished_at, s.total_bytes,
                       COALESCE(s.note, '') AS note,
                       s.allocated_total_bytes,
                       s.unique_allocated_total_bytes,
                       s.measured_allocated_bytes,
                       s.measured_unique_allocated_bytes,
                       s.eligible_file_count,
                       s.allocation_measured_file_count,
                       s.identity_measured_file_count,
                       s.metadata_error_count,
                       s.measurement_state,
                       s.scan_config_version, s.scan_config_json,
                       s.directory_summary_state, s.excluded_rule_count,
                       s.excluded_item_count,
                       {self._snapshot_source_sql()} AS effective_source
                FROM snapshots AS s
                {where_clause}
                ORDER BY s.finished_at DESC, s.id DESC
                LIMIT ? OFFSET ?
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
                       s.allocated_total_bytes,
                       s.unique_allocated_total_bytes,
                       s.measured_allocated_bytes,
                       s.measured_unique_allocated_bytes,
                       s.eligible_file_count,
                       s.allocation_measured_file_count,
                       s.identity_measured_file_count,
                       s.metadata_error_count,
                       s.measurement_state,
                       s.scan_config_version, s.scan_config_json,
                       s.directory_summary_state, s.excluded_rule_count,
                       s.excluded_item_count,
                       {self._snapshot_source_sql()} AS effective_source
                FROM snapshots AS s
                WHERE s.id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_from_row(row) if row else None

    def search_snapshot(
        self,
        snapshot_id: int,
        query: str,
        *,
        mode: str = "prefix",
        kind: str = "any",
        extension: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        limit: int = 100,
        cursor: int = 0,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        self._validate_cursor(cursor)
        snapshot = self.snapshot_info(snapshot_id)
        if snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        if mode not in {"prefix", "substring"}:
            raise ControlError("invalid_args", "mode 必须是 prefix 或 substring")
        if kind not in {"any", "file", "directory"}:
            raise ControlError("invalid_args", "kind 必须是 any、file 或 directory")
        stripped_query = query.strip()
        if not stripped_query:
            raise ControlError("invalid_args", "搜索内容不能为空")
        if mode == "substring" and len(stripped_query) < 3:
            raise ControlError("invalid_args", "子串搜索至少需要 3 个字符")
        if min_size is not None and min_size < 0:
            raise ControlError("invalid_args", "min_size 不能为负数")
        if max_size is not None and max_size < 0:
            raise ControlError("invalid_args", "max_size 不能为负数")
        if min_size is not None and max_size is not None and min_size > max_size:
            raise ControlError("invalid_args", "min_size 不能大于 max_size")

        if mode == "prefix":
            search_path = (
                stripped_query
                if os.path.isabs(stripped_query)
                else os.path.join(snapshot["root_path"], stripped_query)
            )
            normalized_query = os.path.normcase(os.path.abspath(search_path))
            like_pattern = self._escape_like(normalized_query) + "%"
        else:
            like_pattern = "%" + self._escape_like(stripped_query) + "%"

        filters = ["path COLLATE NOCASE LIKE ? ESCAPE '\\'"]
        parameters: list[object] = [like_pattern]
        if kind != "any":
            filters.append("kind = ?")
            parameters.append(kind)
        if extension:
            normalized_extension = extension.strip().lower()
            if not normalized_extension:
                raise ControlError("invalid_args", "extension 不能为空")
            if not normalized_extension.startswith("."):
                normalized_extension = "." + normalized_extension
            if any(character in normalized_extension for character in "*?%_"):
                raise ControlError("invalid_args", "extension 不能包含通配符")
            filters.append("kind = 'file'")
            filters.append("lower(path) LIKE ? ESCAPE '\\'")
            parameters.append("%" + self._escape_like(normalized_extension))
        if min_size is not None:
            filters.append("size_bytes >= ?")
            parameters.append(min_size)
        if max_size is not None:
            filters.append("size_bytes <= ?")
            parameters.append(max_size)
        if modified_after is not None:
            filters.append("modified_at >= ?")
            parameters.append(modified_after)
        if modified_before is not None:
            filters.append("modified_at <= ?")
            parameters.append(modified_before)
        if (
            modified_after is not None
            and modified_before is not None
            and modified_after > modified_before
        ):
            raise ControlError(
                "invalid_args", "modified_after 不能晚于 modified_before"
            )

        if snapshot["directory_summary_state"] == "complete":
            source_sql = """
                SELECT paths.path, 'directory' AS kind,
                       metrics.total_bytes AS size_bytes,
                       metrics.file_count, metrics.modified_at,
                       metrics.allocated_size_bytes,
                       metrics.unique_allocated_size_bytes,
                       metrics.measurement_state
                FROM directory_paths AS paths
                JOIN snapshot_directory_metrics AS metrics
                  ON metrics.path_id = paths.id
                WHERE metrics.snapshot_id = ?
                UNION ALL
                SELECT path, kind, size_bytes, file_count, modified_at,
                       allocated_size_bytes, unique_allocated_size_bytes,
                       measurement_state
                FROM snapshot_items
                WHERE snapshot_id = ? AND kind = 'file'
            """
            source_parameters: list[object] = [snapshot_id, snapshot_id]
            coverage = "目录完整；文件结果受快照 Top N 和明细预算限制"
        else:
            source_sql = """
                SELECT path, kind, size_bytes, file_count, modified_at,
                       allocated_size_bytes, unique_allocated_size_bytes,
                       measurement_state
                FROM snapshot_items WHERE snapshot_id = ?
            """
            source_parameters = [snapshot_id]
            coverage = "旧快照仅搜索已记录的浅层目录和全局大文件"
        query_parameters = source_parameters + parameters + [limit + 1, cursor]
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM ({source_sql})
                WHERE {' AND '.join(filters)}
                ORDER BY size_bytes DESC, path
                LIMIT ? OFFSET ?
                """,
                query_parameters,
            ).fetchall()
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["name"] = os.path.basename(item["path"].rstrip("\\/"))
            items.append(item)
        return {
            "snapshot": snapshot,
            "query": stripped_query,
            "mode": mode,
            "items": items,
            "cursor": cursor,
            "next_cursor": cursor + limit if has_more else None,
            "coverage": coverage,
        }

    def largest_snapshot_files(
        self,
        snapshot_id: int,
        *,
        extension: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        limit: int = 100,
        cursor: int = 0,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        self._validate_cursor(cursor)
        snapshot = self.snapshot_info(snapshot_id)
        if snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        filters = ["snapshot_id = ?", "kind = 'file'"]
        parameters: list[object] = [snapshot_id]
        if extension:
            normalized_extension = extension.strip().lower()
            if not normalized_extension.startswith("."):
                normalized_extension = "." + normalized_extension
            if any(character in normalized_extension for character in "*?%_"):
                raise ControlError("invalid_args", "extension 不能包含通配符")
            filters.append("lower(path) LIKE ? ESCAPE '\\'")
            parameters.append("%" + self._escape_like(normalized_extension))
        for field, value, operator in (
            ("size_bytes", min_size, ">="),
            ("size_bytes", max_size, "<="),
            ("modified_at", modified_after, ">="),
            ("modified_at", modified_before, "<="),
        ):
            if value is not None:
                if field == "size_bytes" and value < 0:
                    raise ControlError("invalid_args", "文件大小不能为负数")
                filters.append(f"{field} {operator} ?")
                parameters.append(value)
        if min_size is not None and max_size is not None and min_size > max_size:
            raise ControlError("invalid_args", "min_size 不能大于 max_size")
        if (
            modified_after is not None
            and modified_before is not None
            and modified_after > modified_before
        ):
            raise ControlError(
                "invalid_args", "modified_after 不能晚于 modified_before"
            )
        parameters.extend((limit + 1, cursor))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT path, parent_path, name, size_bytes, modified_at,
                       allocated_size_bytes, unique_allocated_size_bytes,
                       volume_serial_hex,
                       CASE WHEN file_id IS NULL THEN NULL
                            ELSE lower(hex(file_id)) END AS file_id_hex,
                       link_count, is_unique_owner, measurement_state
                FROM snapshot_items
                WHERE {' AND '.join(filters)}
                ORDER BY size_bytes DESC, path
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            if item["is_unique_owner"] is not None:
                item["is_unique_owner"] = bool(item["is_unique_owner"])
            items.append(item)
        return {
            "snapshot": snapshot,
            "items": items,
            "cursor": cursor,
            "next_cursor": cursor + limit if has_more else None,
            "coverage": "仅覆盖该快照已保存的 Top N 和明细预算内文件",
        }

    def migration_advice(
        self,
        snapshot_id: int,
        target_path: str | Path,
        *,
        extension: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        snapshot = self.snapshot_info(snapshot_id)
        if snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        inspection_limit = min(max(limit * 5, limit), 1_000)
        recorded = self.largest_snapshot_files(
            snapshot_id,
            limit=inspection_limit,
        )
        advice = build_migration_advice(
            recorded["items"],
            target_path,
            active_data_directory=self.database_path.parent,
            extension=extension,
            min_size=min_size,
            max_size=max_size,
            limit=limit,
            inspection_limit=inspection_limit,
        )
        advice["snapshot"] = snapshot
        advice["coverage"] = recorded["coverage"] + "；未额外扫描源盘"
        return advice

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
        self._validate_limit(limit)
        new_snapshot = self.snapshot_info(new_snapshot_id)
        old_snapshot = self.snapshot_info(old_snapshot_id)
        if new_snapshot is None or old_snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        if os.path.normcase(new_snapshot["root_path"]) != os.path.normcase(
            old_snapshot["root_path"]
        ):
            raise ControlError("invalid_args", "只能比较相同路径的快照")
        config_comparison = compare_scan_configs(
            new_snapshot["scan_config_version"],
            new_snapshot["scan_config_json"],
            old_snapshot["scan_config_version"],
            old_snapshot["scan_config_json"],
        )
        if config_comparison["status"] == "mismatch":
            differences = "、".join(config_comparison["differences"])
            raise ControlError(
                "scan_config_mismatch",
                f"扫描配置不同，默认不生成增长归因：{differences}",
            )
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
            "config_comparison": config_comparison,
            "comparison_scope": "shallow_snapshot_items",
            "items": [dict(row) for row in rows],
        }

    def compare_directory_history(
        self,
        new_snapshot_id: int,
        old_snapshot_id: int,
        path: str | None = None,
        *,
        direction: str = "increase",
        limit: int = 100,
    ) -> dict[str, Any]:
        if direction not in {"increase", "decrease"}:
            raise ControlError("invalid_args", "direction 必须是 increase 或 decrease")
        self._validate_limit(limit)
        new_snapshot = self.snapshot_info(new_snapshot_id)
        old_snapshot = self.snapshot_info(old_snapshot_id)
        if new_snapshot is None or old_snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        if os.path.normcase(new_snapshot["root_path"]) != os.path.normcase(
            old_snapshot["root_path"]
        ):
            raise ControlError("invalid_args", "只能比较相同路径的快照")
        config_comparison = compare_scan_configs(
            new_snapshot["scan_config_version"],
            new_snapshot["scan_config_json"],
            old_snapshot["scan_config_version"],
            old_snapshot["scan_config_json"],
        )
        if config_comparison["status"] != "compatible":
            code = (
                "scan_config_mismatch"
                if config_comparison["status"] == "mismatch"
                else "scan_config_unknown"
            )
            differences = "、".join(config_comparison["differences"])
            raise ControlError(code, f"不能生成深层历史归因：{differences}")
        if (
            new_snapshot["directory_summary_state"] != "complete"
            or old_snapshot["directory_summary_state"] != "complete"
        ):
            raise ControlError(
                "directory_history_unavailable",
                "至少一个快照未记录完整目录历史",
            )
        normalized_path = os.path.normcase(
            os.path.abspath(path or new_snapshot["root_path"])
        )
        comparator = ">" if direction == "increase" else "<"
        with self.connection() as connection:
            parent = connection.execute(
                "SELECT id FROM directory_paths WHERE path = ? COLLATE NOCASE",
                (normalized_path,),
            ).fetchone()
            if parent is None:
                raise ControlError("not_found", "指定目录不在该快照中")
            directory_rows = connection.execute(
                f"""
                WITH changes AS (
                    SELECT paths.path,
                           COALESCE(old.total_bytes, 0) AS old_size_bytes,
                           new.total_bytes AS new_size_bytes,
                           old.unique_allocated_size_bytes AS old_unique_bytes,
                           new.unique_allocated_size_bytes AS new_unique_bytes,
                           old.measurement_state AS old_measurement_state,
                           new.measurement_state AS new_measurement_state
                    FROM directory_paths AS paths
                    JOIN snapshot_directory_metrics AS new
                      ON new.path_id = paths.id AND new.snapshot_id = ?
                    LEFT JOIN snapshot_directory_metrics AS old
                      ON old.path_id = paths.id AND old.snapshot_id = ?
                    WHERE paths.parent_id = ?
                    UNION ALL
                    SELECT paths.path, old.total_bytes, 0,
                           old.unique_allocated_size_bytes, NULL,
                           old.measurement_state, NULL
                    FROM directory_paths AS paths
                    JOIN snapshot_directory_metrics AS old
                      ON old.path_id = paths.id AND old.snapshot_id = ?
                    LEFT JOIN snapshot_directory_metrics AS new
                      ON new.path_id = paths.id AND new.snapshot_id = ?
                    WHERE paths.parent_id = ? AND new.path_id IS NULL
                )
                SELECT * FROM changes
                WHERE new_size_bytes {comparator} old_size_bytes
                ORDER BY ABS(new_size_bytes - old_size_bytes) DESC, path
                LIMIT ?
                """,
                (
                    new_snapshot_id,
                    old_snapshot_id,
                    parent["id"],
                    old_snapshot_id,
                    new_snapshot_id,
                    parent["id"],
                    limit,
                ),
            ).fetchall()
            direct_rows = connection.execute(
                """
                SELECT snapshot_id, direct_file_bytes, direct_file_count
                FROM snapshot_directory_metrics
                WHERE path_id = ? AND snapshot_id IN (?, ?)
                """,
                (parent["id"], new_snapshot_id, old_snapshot_id),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in directory_rows:
            item = dict(row)
            item.update(
                {
                    "name": os.path.basename(item["path"].rstrip("\\/")),
                    "kind": "directory",
                    "change_bytes": (
                        item["new_size_bytes"] - item["old_size_bytes"]
                    ),
                }
            )
            if (
                item["new_measurement_state"] == "exact"
                and item["old_measurement_state"] == "exact"
                and item["new_unique_bytes"] is not None
                and item["old_unique_bytes"] is not None
            ):
                item["unique_change_bytes"] = (
                    item["new_unique_bytes"] - item["old_unique_bytes"]
                )
            else:
                item["unique_change_bytes"] = None
            items.append(item)
        direct = {
            int(row["snapshot_id"]): row
            for row in direct_rows
        }
        new_direct = direct.get(new_snapshot_id)
        old_direct = direct.get(old_snapshot_id)
        new_direct_bytes = int(new_direct["direct_file_bytes"]) if new_direct else 0
        old_direct_bytes = int(old_direct["direct_file_bytes"]) if old_direct else 0
        direct_changed = (
            new_direct_bytes > old_direct_bytes
            if direction == "increase"
            else new_direct_bytes < old_direct_bytes
        )
        if direct_changed:
            items.append(
                {
                    "path": None,
                    "name": "未记录文件明细",
                    "kind": "aggregate",
                    "old_size_bytes": old_direct_bytes,
                    "new_size_bytes": new_direct_bytes,
                    "change_bytes": new_direct_bytes - old_direct_bytes,
                    "old_file_count": (
                        int(old_direct["direct_file_count"]) if old_direct else 0
                    ),
                    "new_file_count": (
                        int(new_direct["direct_file_count"]) if new_direct else 0
                    ),
                    "unique_change_bytes": None,
                }
            )
        items.sort(key=lambda item: (-abs(item["change_bytes"]), item["name"]))
        return {
            "new_snapshot": new_snapshot,
            "old_snapshot": old_snapshot,
            "path": normalized_path,
            "direction": direction,
            "config_comparison": config_comparison,
            "comparison_scope": "full_directory_metrics",
            "items": items[:limit],
        }

    def compare_snapshot_accounting(
        self,
        new_snapshot_id: int,
        old_snapshot_id: int,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        with self.connection() as connection:
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
                raise ControlError("not_found", "指定的快照不存在")
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

    def snapshot_tree(
        self,
        snapshot_id: int,
        path: str | None = None,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        snapshot = self.snapshot_info(snapshot_id)
        if snapshot is None:
            raise ControlError("not_found", "指定的快照不存在")
        normalized_path = os.path.normcase(
            os.path.abspath(path or snapshot["root_path"])
        )
        if snapshot["directory_summary_state"] != "complete":
            return self._shallow_snapshot_tree(
                snapshot, normalized_path, limit=limit
            )
        with self.connection() as connection:
            current = connection.execute(
                """
                SELECT paths.id AS path_id, paths.path,
                       metrics.total_bytes, metrics.allocated_size_bytes,
                       metrics.unique_allocated_size_bytes,
                       metrics.measured_allocated_bytes,
                       metrics.measured_unique_allocated_bytes,
                       metrics.file_count, metrics.directory_count,
                       metrics.direct_file_bytes, metrics.direct_file_count,
                       metrics.eligible_file_count,
                       metrics.allocation_measured_file_count,
                       metrics.identity_measured_file_count,
                       metrics.metadata_error_count, metrics.error_count,
                       metrics.modified_at, metrics.measurement_state
                FROM directory_paths AS paths
                JOIN snapshot_directory_metrics AS metrics
                  ON metrics.path_id = paths.id
                WHERE metrics.snapshot_id = ?
                  AND paths.path = ? COLLATE NOCASE
                """,
                (snapshot_id, normalized_path),
            ).fetchone()
            if current is None:
                raise ControlError("not_found", "指定目录不在该快照中")
            reserve_aggregate = int(current["direct_file_count"]) > 0
            candidate_limit = limit - 1 if reserve_aggregate else limit
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT child.path, ? AS parent_path, 'directory' AS kind,
                           child_metrics.total_bytes AS size_bytes,
                           child_metrics.file_count, child_metrics.modified_at,
                           child_metrics.allocated_size_bytes,
                           child_metrics.unique_allocated_size_bytes,
                           child_metrics.measured_allocated_bytes,
                           child_metrics.measured_unique_allocated_bytes,
                           child_metrics.eligible_file_count,
                           child_metrics.allocation_measured_file_count,
                           child_metrics.identity_measured_file_count,
                           child_metrics.metadata_error_count,
                           child_metrics.error_count,
                           child_metrics.measurement_state,
                           NULL AS volume_serial_hex, NULL AS file_id_hex,
                           NULL AS link_count, NULL AS is_unique_owner
                    FROM directory_paths AS child
                    JOIN snapshot_directory_metrics AS child_metrics
                      ON child_metrics.path_id = child.id
                    WHERE child_metrics.snapshot_id = ?
                      AND child.parent_id = ?
                    UNION ALL
                    SELECT item.path, item.parent_path, item.kind,
                           item.size_bytes, item.file_count, item.modified_at,
                           item.allocated_size_bytes,
                           item.unique_allocated_size_bytes,
                           COALESCE(item.allocated_size_bytes, 0),
                           COALESCE(item.unique_allocated_size_bytes, 0),
                           1, CASE WHEN item.allocated_size_bytes IS NULL THEN 0 ELSE 1 END,
                           CASE WHEN item.file_id IS NULL THEN 0 ELSE 1 END,
                           CASE WHEN item.measurement_state IN ('exact', 'legacy') THEN 0 ELSE 1 END,
                           0, item.measurement_state,
                           item.volume_serial_hex,
                           CASE WHEN item.file_id IS NULL THEN NULL
                                ELSE lower(hex(item.file_id)) END,
                           item.link_count, item.is_unique_owner
                    FROM snapshot_items AS item
                    WHERE item.snapshot_id = ? AND item.kind = 'file'
                      AND item.parent_path = ? COLLATE NOCASE
                )
                ORDER BY size_bytes DESC, path
                LIMIT ?
                """,
                (
                    normalized_path,
                    snapshot_id,
                    current["path_id"],
                    snapshot_id,
                    normalized_path,
                    max(candidate_limit, 0),
                ),
            ).fetchall()
        items = []
        visible_file_bytes = 0
        visible_file_count = 0
        for row in rows:
            item = dict(row)
            item["name"] = os.path.basename(item["path"].rstrip("\\/"))
            if item["is_unique_owner"] is not None:
                item["is_unique_owner"] = bool(item["is_unique_owner"])
            if item["kind"] == "file":
                visible_file_bytes += int(item["size_bytes"])
                visible_file_count += int(item["file_count"])
            items.append(item)
        hidden_file_bytes = max(
            int(current["direct_file_bytes"]) - visible_file_bytes, 0
        )
        hidden_file_count = max(
            int(current["direct_file_count"]) - visible_file_count, 0
        )
        if hidden_file_count > 0 or hidden_file_bytes > 0:
            items.append(
                {
                    "path": None,
                    "parent_path": normalized_path,
                    "name": "未记录文件明细",
                    "kind": "aggregate",
                    "size_bytes": hidden_file_bytes,
                    "file_count": hidden_file_count,
                    "modified_at": None,
                    "allocated_size_bytes": None,
                    "unique_allocated_size_bytes": None,
                    "measurement_state": "aggregate",
                }
            )
        return {
            "snapshot": snapshot,
            "path": normalized_path,
            "node": dict(current),
            "items": items,
            "data_source": "full_directory_metrics",
            "coverage": "目录汇总完整；文件明细受快照记录预算限制",
        }

    def _shallow_snapshot_tree(
        self,
        snapshot: dict[str, Any],
        normalized_path: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT path, parent_path, name, kind, size_bytes,
                       file_count, depth, modified_at,
                       allocated_size_bytes, unique_allocated_size_bytes,
                       volume_serial_hex,
                       CASE WHEN file_id IS NULL THEN NULL
                            ELSE lower(hex(file_id)) END AS file_id_hex,
                       link_count, is_unique_owner, measurement_state
                FROM snapshot_items
                WHERE snapshot_id = ? AND parent_path = ? COLLATE NOCASE
                ORDER BY size_bytes DESC, path
                LIMIT ?
                """,
                (snapshot["id"], normalized_path, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if item["is_unique_owner"] is not None:
                item["is_unique_owner"] = bool(item["is_unique_owner"])
            items.append(item)
        state_messages = {
            "legacy": "旧快照未记录深层目录",
            "not_saved": "此类快照仅保存浅层目录和全局大文件",
            "unavailable": "扫描时未保留完整目录骨架",
            "expired": "完整目录指标已超过保留期",
        }
        return {
            "snapshot": snapshot,
            "path": normalized_path,
            "items": items,
            "data_source": "shallow_snapshot_items",
            "coverage": state_messages.get(
                snapshot["directory_summary_state"],
                "快照未记录完整目录历史",
            ),
        }
