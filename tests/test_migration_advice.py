from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from disk_monitor.migration_advice import SAFETY_NOTICE, build_migration_advice


def recorded_file(path: str, size: int = 500) -> dict:
    return {
        "path": path,
        "kind": "file",
        "size_bytes": size,
        "modified_at": 100.0,
        "allocated_size_bytes": 512,
        "unique_allocated_size_bytes": 512,
        "file_id_hex": "01" * 16,
        "link_count": 1,
        "is_unique_owner": True,
        "measurement_state": "exact",
    }


class MigrationAdviceTests(unittest.TestCase):
    def test_candidates_are_read_only_bounded_and_conservatively_excluded(self) -> None:
        items = [
            {
                "path": r"C:\Users\Alice\Videos",
                "kind": "directory",
                "size_bytes": 1_000,
                "modified_at": 100.0,
            },
            recorded_file(r"C:\Users\Alice\Videos\large.mkv"),
            {**recorded_file(r"C:\Users\Alice\Videos\alias.mkv"), "link_count": 2},
            recorded_file(r"C:\Users\Alice\cache.db"),
            recorded_file(r"C:\Users\Alice\missing.mkv"),
            recorded_file(r"C:\Windows\Logs\system.log"),
            recorded_file(r"C:\Users\Alice\cloud.mkv"),
        ]
        normalized = {
            os.path.normcase(os.path.abspath(item["path"])): SimpleNamespace(
                st_size=item["size_bytes"],
                st_mtime=item["modified_at"],
                st_file_attributes=(0x1000 if "cloud" in item["path"] else 0),
            )
            for item in items
            if "missing" not in item["path"]
        }
        usage_calls = []

        def read_usage(path: str) -> SimpleNamespace:
            usage_calls.append(path)
            return SimpleNamespace(total=10_000, used=2_000, free=8_000)

        def read_stat(path: str, **_kwargs) -> SimpleNamespace:
            try:
                return normalized[path]
            except KeyError as error:
                raise FileNotFoundError(path) from error

        advice = build_migration_advice(
            items,
            "D:/",
            active_data_directory=r"C:\Users\Alice\AppData\Local\DiskGrowthMonitor",
            limit=10,
            disk_usage_reader=read_usage,
            stat_reader=read_stat,
        )

        self.assertEqual(usage_calls, [os.path.normcase(os.path.abspath("D:/"))])
        self.assertEqual(advice["notice"], SAFETY_NOTICE)
        self.assertEqual(len(advice["candidates"]), 1)
        self.assertEqual(advice["candidates"][0]["estimated_size_bytes"], 512)
        reasons = {
            Path(item["path"]).name: set(item["reason_codes"])
            for item in advice["excluded"]
        }
        self.assertIn("directory", reasons["videos"])
        self.assertIn("hard_link", reasons["alias.mkv"])
        self.assertIn("app_managed_type", reasons["cache.db"])
        self.assertIn("missing", reasons["missing.mkv"])
        self.assertIn("system_or_app_data", reasons["system.log"])
        self.assertIn("cloud_or_offline", reasons["cloud.mkv"])
        self.assertTrue(advice["target"]["space_sufficient"])
        self.assertEqual(advice["target"]["estimated_remaining_bytes"], 7488)

    def test_insufficient_target_space_is_reported_without_file_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.bin"
            path.write_bytes(b"x" * 20)
            item = recorded_file(str(path), 20)
            item["modified_at"] = path.stat().st_mtime
            item["unique_allocated_size_bytes"] = 30

            advice = build_migration_advice(
                [item],
                r"Z:\target",
                disk_usage_reader=lambda _path: SimpleNamespace(
                    total=100, used=90, free=10
                ),
            )

        self.assertFalse(advice["target"]["space_sufficient"])
        self.assertEqual(advice["target"]["estimated_remaining_bytes"], -20)
        self.assertNotIn("action", advice["candidates"][0])


if __name__ == "__main__":
    unittest.main()
