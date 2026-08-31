from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from disk_monitor.navigation import (
    enforce_skeleton_budget,
    materialize_navigation_result,
    merge_directory_skeleton,
)
from disk_monitor.scanner import scan_path
from disk_monitor.windows_file_info import FileSpaceInfo


class ScannerTests(unittest.TestCase):
    def test_exclusion_rules_skip_before_accounting_and_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            cache = root / "cache"
            cache.mkdir(parents=True)
            (root / "keep.bin").write_bytes(b"k" * 7)
            (root / "ignored.tmp").write_bytes(b"t" * 11)
            (cache / "nested.bin").write_bytes(b"n" * 13)

            result = scan_path(
                str(root),
                exclude_rules=(str(cache), "*.tmp"),
            )

        self.assertEqual(result.total_bytes, 7)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.directory_count, 1)
        self.assertEqual(result.excluded_rule_count, 2)
        self.assertEqual(result.excluded_item_count, 2)
        self.assertIn('"exclude_rules":["path:', result.scan_config_json)
        self.assertIn('glob:*.tmp', result.scan_config_json)

    def test_invalid_exclusion_rule_identifies_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "root"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "第 2 行"):
                scan_path(
                    str(root),
                    exclude_rules=("*.tmp", str(base / "outside")),
                )

    def test_nested_glob_does_not_exclude_its_literal_prefix_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            deep = root / "group" / "deep"
            deep.mkdir(parents=True)
            (root / "group" / "keep.bin").write_bytes(b"k" * 7)
            (deep / "secret.bin").write_bytes(b"s" * 11)

            result = scan_path(
                str(root), exclude_rules=("group/**/secret.bin",)
            )

        self.assertEqual(result.total_bytes, 7)
        self.assertEqual(result.directory_count, 3)
        self.assertEqual(result.excluded_item_count, 1)

    def test_file_space_accounting_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data.bin"
            path.write_bytes(b"x" * 17)

            result = scan_path(temp_dir)

        self.assertEqual(result.measurement_state, "legacy")
        self.assertIsNone(result.allocated_total_bytes)
        self.assertIsNone(result.unique_allocated_total_bytes)
        self.assertEqual(result.eligible_file_count, 0)

    def test_file_space_metadata_failure_keeps_logical_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data.bin"
            path.write_bytes(b"x" * 17)
            unavailable = FileSpaceInfo(
                None, None, None, None, "inaccessible", 5
            )

            with patch(
                "disk_monitor.scanner.read_file_space_info",
                return_value=unavailable,
            ):
                result = scan_path(temp_dir, collect_file_space=True)

        self.assertEqual(result.total_bytes, 17)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.measurement_state, "unavailable")
        self.assertIsNone(result.allocated_total_bytes)
        self.assertIsNone(result.unique_allocated_total_bytes)
        self.assertEqual(result.metadata_error_count, 1)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 提供硬链接统计")
    def test_hard_links_have_deterministic_unique_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_directory = root / "a"
            later_directory = root / "z"
            first_directory.mkdir()
            later_directory.mkdir()
            original = later_directory / "original.bin"
            alias = first_directory / "alias.bin"
            original.write_bytes(b"x" * 8193)

            before_alias = scan_path(
                temp_dir, record_depth=2, collect_file_space=True
            )
            os.link(original, alias)
            normal = scan_path(
                temp_dir, record_depth=2, collect_file_space=True
            )

            real_scandir = os.scandir

            class ReversedScandir:
                def __init__(self, directory: str) -> None:
                    self.iterator = real_scandir(directory)

                def __enter__(self):
                    return iter(reversed(list(self.iterator)))

                def __exit__(self, exc_type, exc_value, traceback) -> None:
                    del exc_type, exc_value, traceback
                    self.iterator.close()

            with patch(
                "disk_monitor.scanner.os.scandir",
                side_effect=ReversedScandir,
            ):
                reversed_result = scan_path(
                    temp_dir,
                    record_depth=2,
                    collect_file_space=True,
                )

        self.assertEqual(before_alias.measurement_state, "exact")
        self.assertEqual(normal.measurement_state, "exact")
        self.assertEqual(reversed_result.measurement_state, "exact")
        self.assertEqual(normal.total_bytes, 8193 * 2)
        self.assertEqual(normal.file_count, 2)
        self.assertEqual(
            before_alias.unique_allocated_total_bytes,
            normal.unique_allocated_total_bytes,
        )
        self.assertEqual(
            normal.unique_allocated_total_bytes,
            reversed_result.unique_allocated_total_bytes,
        )
        self.assertIsNotNone(normal.unique_allocated_total_bytes)
        assert normal.unique_allocated_total_bytes is not None
        self.assertEqual(
            normal.allocated_total_bytes,
            normal.unique_allocated_total_bytes * 2,
        )

        normal_files = {
            item.path: item for item in normal.items if item.kind == "file"
        }
        reversed_files = {
            item.path: item
            for item in reversed_result.items
            if item.kind == "file"
        }
        alias_path = os.path.normcase(os.path.abspath(alias))
        original_path = os.path.normcase(os.path.abspath(original))
        self.assertTrue(normal_files[alias_path].is_unique_owner)
        self.assertFalse(normal_files[original_path].is_unique_owner)
        self.assertTrue(reversed_files[alias_path].is_unique_owner)
        self.assertFalse(reversed_files[original_path].is_unique_owner)
        self.assertEqual(
            normal_files[alias_path].unique_allocated_size_bytes,
            normal.unique_allocated_total_bytes,
        )
        self.assertEqual(
            normal_files[original_path].unique_allocated_size_bytes,
            0,
        )

        directories = {
            item.path: item
            for item in normal.items
            if item.kind == "directory"
        }
        first_path = os.path.normcase(os.path.abspath(first_directory))
        later_path = os.path.normcase(os.path.abspath(later_directory))
        self.assertEqual(
            directories[first_path].unique_allocated_size_bytes,
            normal.unique_allocated_total_bytes,
        )
        self.assertEqual(
            directories[later_path].unique_allocated_size_bytes,
            0,
        )
        assert normal.skeleton is not None
        root_navigation = materialize_navigation_result(
            normal.skeleton, temp_dir
        )
        first_navigation = materialize_navigation_result(
            normal.skeleton, str(first_directory)
        )
        later_navigation = materialize_navigation_result(
            normal.skeleton, str(later_directory)
        )
        assert root_navigation is not None
        assert first_navigation is not None
        assert later_navigation is not None
        self.assertEqual(root_navigation.measurement_state, "exact")
        self.assertEqual(
            root_navigation.unique_allocated_total_bytes,
            normal.unique_allocated_total_bytes,
        )
        self.assertEqual(
            first_navigation.unique_allocated_total_bytes,
            normal.unique_allocated_total_bytes,
        )
        self.assertEqual(later_navigation.unique_allocated_total_bytes, 0)

    def test_scan_handles_directory_depth_beyond_python_recursion_limit(self) -> None:
        root = os.path.normcase(os.path.abspath("virtual-root"))
        depths = {root: 0}
        target_depth = 1_100

        class FakeEntry:
            def __init__(self, parent: str, name: str, *, is_directory: bool) -> None:
                self.name = name
                self.path = os.path.join(parent, name)
                self._is_directory = is_directory

            def stat(self, *, follow_symlinks: bool = False):
                del follow_symlinks
                return SimpleNamespace(
                    st_file_attributes=0,
                    st_size=7,
                    st_mtime=1.0,
                )

            def is_symlink(self) -> bool:
                return False

            def is_dir(self, *, follow_symlinks: bool = False) -> bool:
                del follow_symlinks
                return self._is_directory

            def is_file(self, *, follow_symlinks: bool = False) -> bool:
                del follow_symlinks
                return not self._is_directory

        class FakeScandir:
            def __init__(self, entries) -> None:
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                del exc_type, exc_value, traceback

        def fake_scandir(directory: str):
            normalized = os.path.normcase(os.path.abspath(directory))
            depth = depths[normalized]
            if depth < target_depth:
                entry = FakeEntry(normalized, "d", is_directory=True)
                depths[os.path.normcase(os.path.abspath(entry.path))] = depth + 1
                return FakeScandir([entry])
            return FakeScandir([FakeEntry(normalized, "leaf.bin", is_directory=False)])

        with patch("disk_monitor.scanner.os.path.isdir", return_value=True), patch(
            "disk_monitor.scanner.os.scandir", side_effect=fake_scandir
        ):
            result = scan_path(root, record_depth=1)

        self.assertEqual(result.total_bytes, 7)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.directory_count, target_depth + 1)
        self.assertEqual(result.error_count, 0)

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
