from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from disk_monitor.navigation import (
    enforce_skeleton_budget,
    materialize_navigation_result,
    merge_directory_skeleton,
)
from disk_monitor.scanner import scan_path


class ScannerTests(unittest.TestCase):
    def test_scan_aggregates_files_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "direct.bin").write_bytes(b"a" * 100)
            nested = root / "nested"
            nested.mkdir()
            (nested / "child.bin").write_bytes(b"b" * 30)

            result = scan_path(str(root), record_depth=2, top_file_limit=10)

            self.assertEqual(result.total_bytes, 130)
            self.assertEqual(result.file_count, 2)
            self.assertEqual(result.directory_count, 2)
            nested_path = os.path.normcase(os.path.abspath(nested))
            nested_item = next(item for item in result.items if item.path == nested_path)
            self.assertEqual(nested_item.size_bytes, 30)
            self.assertEqual(nested_item.file_count, 1)

    def test_navigation_skeleton_opens_deep_directory_without_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            (deep / "large.bin").write_bytes(b"x" * 50)
            (deep / "small.bin").write_bytes(b"x" * 10)

            result = scan_path(
                str(root), record_depth=1, navigation_file_limit=1
            )

            self.assertIsNotNone(result.skeleton)
            assert result.skeleton is not None
            deep_result = materialize_navigation_result(
                result.skeleton, str(deep)
            )
            self.assertIsNotNone(deep_result)
            assert deep_result is not None
            self.assertEqual(deep_result.total_bytes, 60)
            self.assertEqual(deep_result.file_count, 2)
            visible = {item.kind: item for item in deep_result.items}
            self.assertEqual(visible["file"].name, "large.bin")
            self.assertEqual(visible["aggregate"].size_bytes, 10)
            self.assertEqual(visible["aggregate"].file_count, 1)

    def test_local_rescan_updates_skeleton_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deep = root / "parent" / "deep"
            deep.mkdir(parents=True)
            data_file = deep / "data.bin"
            data_file.write_bytes(b"x" * 10)
            initial = scan_path(str(root))
            data_file.write_bytes(b"x" * 35)
            update = scan_path(str(deep))

            assert initial.skeleton is not None
            assert update.skeleton is not None
            merged = merge_directory_skeleton(
                initial.skeleton, update.skeleton
            )
            root_result = materialize_navigation_result(merged, str(root))
            parent_result = materialize_navigation_result(
                merged, str(root / "parent")
            )

            assert root_result is not None
            assert parent_result is not None
            self.assertEqual(root_result.total_bytes, 35)
            self.assertEqual(parent_result.total_bytes, 35)
            self.assertEqual(
                next(
                    item
                    for item in root_result.items
                    if item.kind == "directory"
                    and item.path != root_result.root_path
                ).size_bytes,
                35,
            )

    def test_navigation_budget_drops_file_details_before_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child = root / "child"
            child.mkdir()
            for index in range(20):
                (child / f"file-{index}.bin").write_bytes(b"x" * (index + 1))
            result = scan_path(
                str(root), navigation_file_limit=20, navigation_memory_budget=10**9
            )
            assert result.skeleton is not None
            full_size = result.skeleton.estimated_bytes

            compacted = enforce_skeleton_budget(
                result.skeleton, budget_bytes=full_size - 1
            )
            child_result = materialize_navigation_result(compacted, str(child))

            self.assertTrue(compacted.degraded)
            self.assertTrue(any(
                item.kind == "directory"
                for item in materialize_navigation_result(compacted, str(root)).items
            ))
            assert child_result is not None
            self.assertFalse(any(item.kind == "file" for item in child_result.items))
            aggregate = next(
                item for item in child_result.items if item.kind == "aggregate"
            )
            self.assertEqual(aggregate.file_count, 20)

    def test_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "data.bin").write_bytes(b"x" * 25)
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("当前 Windows 环境不允许创建符号链接")

            result = scan_path(str(root))
            self.assertEqual(result.total_bytes, 25)
            self.assertFalse(any(item.name == "link" for item in result.items))


if __name__ == "__main__":
    unittest.main()
