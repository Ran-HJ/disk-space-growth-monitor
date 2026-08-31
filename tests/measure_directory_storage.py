from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from disk_monitor.scanner import scan_path


DIRECTORY_SCHEMA = """
CREATE TABLE snapshot_directories (
    snapshot_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    name TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    allocated_size_bytes INTEGER,
    unique_allocated_size_bytes INTEGER,
    file_count INTEGER NOT NULL,
    directory_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    modified_at REAL NOT NULL,
    measurement_state TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, path)
);
CREATE INDEX idx_snapshot_directories_parent
    ON snapshot_directories(snapshot_id, parent_path);
"""

CANDIDATE_SCHEMA = """
CREATE TABLE directory_paths (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL COLLATE NOCASE UNIQUE,
    parent_id INTEGER
);
CREATE INDEX idx_directory_paths_parent
    ON directory_paths(parent_id, path);
CREATE TABLE snapshot_directory_metrics (
    snapshot_id INTEGER NOT NULL,
    path_id INTEGER NOT NULL,
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
) WITHOUT ROWID;
"""


def measure_root(label: str, root: Path, temporary_directory: Path) -> dict:
    result = scan_path(
        str(root),
        record_depth=1,
        top_file_limit=0,
        navigation_file_limit=0,
        navigation_total_file_limit=0,
        collect_file_space=False,
    )
    if result.skeleton is None:
        raise RuntimeError(f"{label} 扫描没有生成目录骨架")

    nodes = result.skeleton.nodes
    database_path = temporary_directory / f"{label}.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(DIRECTORY_SCHEMA)
        connection.commit()
        schema_bytes = database_path.stat().st_size
        connection.executemany(
            """
            INSERT INTO snapshot_directories(
                snapshot_id, path, parent_path, name, total_bytes,
                allocated_size_bytes, unique_allocated_size_bytes,
                file_count, directory_count, error_count, modified_at,
                measurement_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    path,
                    os.path.dirname(path),
                    os.path.basename(path.rstrip("\\/")) or path,
                    node.total_bytes,
                    node.allocated_size_bytes,
                    node.unique_allocated_size_bytes,
                    node.file_count,
                    node.directory_count,
                    node.error_count,
                    node.modified_at,
                    node.measurement_state,
                )
                for path, node in nodes.items()
            ],
        )
        connection.commit()
        stored_bytes = database_path.stat().st_size - schema_bytes
    finally:
        connection.close()

    directory_count = len(nodes)
    path_lengths = [len(path) for path in nodes]
    path_utf8_lengths = [len(path.encode("utf-8")) for path in nodes]
    return {
        "label": label,
        "root_path": result.root_path,
        "file_count": result.file_count,
        "directory_count": directory_count,
        "scan_error_count": result.error_count,
        "average_path_characters": round(sum(path_lengths) / directory_count, 2),
        "average_path_utf8_bytes": round(
            sum(path_utf8_lengths) / directory_count, 2
        ),
        "maximum_path_characters": max(path_lengths),
        "sqlite_snapshot_increment_bytes": stored_bytes,
        "sqlite_bytes_per_directory": round(stored_bytes / directory_count, 2),
        "estimated_storage_bytes": {
            "7_days_1_snapshot_per_day": stored_bytes * 7,
            "30_days_1_snapshot_per_day": stored_bytes * 30,
            "90_days_1_snapshot_per_day": stored_bytes * 90,
            "7_days_2_snapshots_per_day": stored_bytes * 14,
            "30_days_2_snapshots_per_day": stored_bytes * 60,
            "90_days_2_snapshots_per_day": stored_bytes * 180,
        },
    }


def build_small_fixture(root: Path) -> None:
    for relative in ("first", "first/deep", "second", "empty"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "root.bin").write_bytes(b"r" * 64)
    (root / "first" / "one.bin").write_bytes(b"a" * 128)
    (root / "first" / "deep" / "two.bin").write_bytes(b"b" * 256)
    (root / "second" / "three.bin").write_bytes(b"c" * 512)


def synthetic_path(index: int, target_length: int) -> str:
    prefix = f"c:\\synthetic\\{index:08d}\\"
    return prefix + ("x" * max(target_length - len(prefix), 1))


def measure_candidate_schema(
    directory_count: int,
    average_path_length: int,
    temporary_directory: Path,
) -> dict:
    database_path = temporary_directory / "candidate.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(CANDIDATE_SCHEMA)
        connection.commit()
        schema_bytes = database_path.stat().st_size
        connection.executemany(
            "INSERT INTO directory_paths(id, path, parent_id) VALUES (?, ?, ?)",
            (
                (
                    index,
                    synthetic_path(index, average_path_length),
                    ((index - 2) // 10) + 1 if index > 1 else None,
                )
                for index in range(1, directory_count + 1)
            ),
        )
        connection.commit()
        dictionary_bytes = database_path.stat().st_size - schema_bytes

        def insert_metrics(snapshot_id: int) -> None:
            connection.executemany(
                """
                INSERT INTO snapshot_directory_metrics(
                    snapshot_id, path_id, total_bytes,
                    allocated_size_bytes, unique_allocated_size_bytes,
                    measured_allocated_bytes,
                    measured_unique_allocated_bytes,
                    file_count, directory_count,
                    direct_file_bytes, direct_file_count,
                    eligible_file_count, allocation_measured_file_count,
                    identity_measured_file_count, metadata_error_count,
                    error_count,
                    modified_at, measurement_state
                ) VALUES (
                    ?, ?, ?, NULL, NULL, 0, 0, ?, ?, ?, ?, 0, 0, 0, 0,
                    0, ?, 'legacy'
                )
                """,
                (
                    (
                        snapshot_id,
                        path_id,
                        path_id * 4096,
                        path_id % 1000,
                        path_id % 100,
                        (path_id % 10) * 4096,
                        path_id % 10,
                        1_700_000_000.0 + path_id,
                    )
                    for path_id in range(1, directory_count + 1)
                ),
            )
            connection.commit()

        before_first_snapshot = database_path.stat().st_size
        insert_metrics(1)
        first_snapshot_bytes = (
            database_path.stat().st_size - before_first_snapshot
        )
        before_second_snapshot = database_path.stat().st_size
        insert_metrics(2)
        second_snapshot_bytes = (
            database_path.stat().st_size - before_second_snapshot
        )

        def measured_query(sql: str, parameters: tuple[object, ...]) -> dict:
            plan = [
                row[3]
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, parameters
                ).fetchall()
            ]
            started = time.perf_counter()
            rows = connection.execute(sql, parameters).fetchall()
            elapsed_ms = (time.perf_counter() - started) * 1000
            return {
                "elapsed_ms": round(elapsed_ms, 3),
                "row_count": len(rows),
                "query_plan": plan,
            }

        query_checks = {
            "deep_children": measured_query(
                """
                SELECT paths.path, metrics.total_bytes
                FROM directory_paths AS paths
                JOIN snapshot_directory_metrics AS metrics
                  ON metrics.path_id = paths.id
                WHERE metrics.snapshot_id = ? AND paths.parent_id = ?
                ORDER BY metrics.total_bytes DESC, paths.path
                LIMIT 200
                """,
                (2, max(directory_count // 20, 1)),
            ),
            "path_prefix": measured_query(
                """
                SELECT path FROM directory_paths
                WHERE path LIKE ? ESCAPE '\\'
                ORDER BY path LIMIT 200
                """,
                ("c:\\\\synthetic\\\\001%",),
            ),
            "path_substring_no_match": measured_query(
                """
                SELECT path FROM directory_paths
                WHERE path LIKE ? ESCAPE '\\'
                ORDER BY path LIMIT 200
                """,
                ("%not-present%",),
            ),
            "late_page": measured_query(
                "SELECT path FROM directory_paths ORDER BY path LIMIT 200 OFFSET ?",
                (max(directory_count - 200, 0),),
            ),
        }
    finally:
        connection.close()

    return {
        "directory_count": directory_count,
        "synthetic_path_characters": average_path_length,
        "one_time_path_dictionary_bytes": dictionary_bytes,
        "first_snapshot_metrics_bytes": first_snapshot_bytes,
        "second_snapshot_metrics_bytes": second_snapshot_bytes,
        "steady_state_bytes_per_directory": round(
            second_snapshot_bytes / directory_count, 2
        ),
        "query_checks": query_checks,
        "estimated_total_bytes": {
            "default_auto_7_baseline_30_closing": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 36
            ),
            "7_days_1_snapshot_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 6
            ),
            "30_days_1_snapshot_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 29
            ),
            "90_days_1_snapshot_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 89
            ),
            "7_days_2_snapshots_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 13
            ),
            "30_days_2_snapshots_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 59
            ),
            "90_days_2_snapshots_per_day": (
                dictionary_bytes + first_snapshot_bytes
                + second_snapshot_bytes * 179
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure candidate full-directory snapshot storage once."
    )
    parser.add_argument("--medium", type=Path)
    parser.add_argument("--real", type=Path)
    parser.add_argument("--candidate-count", type=int)
    parser.add_argument("--candidate-path-length", type=int)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="DiskMonitorDirectoryScale-") as temp:
        temporary_directory = Path(temp)
        if args.candidate_count is not None:
            if args.candidate_path_length is None:
                parser.error("--candidate-count 需要 --candidate-path-length")
            candidate = measure_candidate_schema(
                args.candidate_count,
                args.candidate_path_length,
                temporary_directory,
            )
            print(json.dumps({"candidate": candidate}, indent=2))
            return 0
        if args.medium is None or args.real is None:
            parser.error("完整测量需要 --medium 和 --real")
        small_root = temporary_directory / "small-fixture"
        build_small_fixture(small_root)
        measurements = [
            measure_root("small", small_root, temporary_directory),
            measure_root("medium", args.medium.resolve(), temporary_directory),
            measure_root("real_c_drive", args.real.resolve(), temporary_directory),
        ]
        candidate = measure_candidate_schema(
            measurements[-1]["directory_count"],
            round(measurements[-1]["average_path_characters"]),
            temporary_directory,
        )
    print(
        json.dumps(
            {"measurements": measurements, "candidate": candidate},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
