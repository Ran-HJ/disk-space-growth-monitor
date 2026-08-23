from __future__ import annotations

import heapq
import os
import stat
import threading
from collections.abc import Callable
from datetime import datetime
from itertools import count

from .models import (
    DirectorySkeleton,
    NavigationItem,
    NavigationNode,
    ScanItem,
    ScanProgress,
    ScanResult,
)
from .navigation import NAVIGATION_MEMORY_BUDGET_BYTES, enforce_skeleton_budget


ProgressCallback = Callable[[ScanProgress], None]


class ScanCancelled(Exception):
    """扫描被用户取消。"""


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def scan_path(
    root_path: str,
    *,
    record_depth: int = 2,
    top_file_limit: int = 200,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
    navigation_file_limit: int = 20,
    navigation_total_file_limit: int = 20_000,
    navigation_memory_budget: int = NAVIGATION_MEMORY_BUDGET_BYTES,
) -> ScanResult:
    """扫描目录，保留数据库快照项目并构建会话级导航骨架。

    导航骨架保存全部目录、每目录有限大文件及其余文件聚合统计。
    """

    root = os.path.normcase(os.path.abspath(root_path))
    if not os.path.isdir(root):
        raise ValueError(f"扫描路径不是可读取目录：{root_path}")
    if record_depth < 1:
        raise ValueError("record_depth 必须至少为 1")
    if navigation_file_limit < 0 or navigation_total_file_limit < 0:
        raise ValueError("导航文件数量限制不能为负数")
    if navigation_memory_budget < 1:
        raise ValueError("导航内存预算必须至少为 1")

    started_at = datetime.now()
    cancel = cancel_event or threading.Event()
    items: dict[str, ScanItem] = {}
    top_files: list[tuple[int, int, ScanItem]] = []
    sequence = count()
    navigation_sequence = count()
    navigation_nodes: dict[str, NavigationNode] = {}
    navigation_reserved_slots = 0
    navigation_committed_slots = 0
    counters = {
        "bytes": 0,
        "files": 0,
        "directories": 0,
        "errors": 0,
        "since_progress": 0,
    }

    def report(current_path: str, *, force: bool = False) -> None:
        if progress_callback is None:
            return
        if not force and counters["since_progress"] < 250:
            return
        counters["since_progress"] = 0
        progress_callback(
            ScanProgress(
                current_path=current_path,
                bytes_seen=counters["bytes"],
                file_count=counters["files"],
                directory_count=counters["directories"],
                error_count=counters["errors"],
            )
        )

    def remember_file(item: ScanItem) -> None:
        if top_file_limit <= 0:
            return
        entry = (item.size_bytes, next(sequence), item)
        if len(top_files) < top_file_limit:
            heapq.heappush(top_files, entry)
        elif item.size_bytes > top_files[0][0]:
            heapq.heapreplace(top_files, entry)

    def reserve_navigation_slots() -> int:
        nonlocal navigation_reserved_slots
        available = max(
            navigation_total_file_limit
            - navigation_committed_slots
            - navigation_reserved_slots,
            0,
        )
        reserved = min(navigation_file_limit, available)
        navigation_reserved_slots += reserved
        return reserved

    def commit_navigation_slots(reserved: int, used: int) -> None:
        nonlocal navigation_reserved_slots, navigation_committed_slots
        navigation_reserved_slots -= reserved
        navigation_committed_slots += used

    def walk(directory: str, depth: int) -> tuple[int, int, int, int, float]:
        if cancel.is_set():
            raise ScanCancelled

        normalized_directory = os.path.normcase(os.path.abspath(directory))
        total_size = 0
        file_count = 0
        directory_count = 1
        error_count = 0
        latest_modified = 0.0
        direct_file_bytes = 0
        direct_file_count = 0
        directory_children: list[NavigationItem] = []
        navigation_file_slots = reserve_navigation_slots()
        navigation_files: list[tuple[int, int, str, float]] = []
        counters["directories"] += 1

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel.is_set():
                        raise ScanCancelled
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or _is_reparse_point(entry_stat):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            (
                                child_size,
                                child_files,
                                child_directories,
                                child_errors,
                                child_modified,
                            ) = walk(entry.path, depth + 1)
                            total_size += child_size
                            file_count += child_files
                            directory_count += child_directories
                            error_count += child_errors
                            latest_modified = max(latest_modified, child_modified)
                            directory_children.append(
                                NavigationItem(
                                    name=entry.name,
                                    kind="directory",
                                    size_bytes=child_size,
                                    file_count=child_files,
                                    modified_at=child_modified,
                                )
                            )
                            if depth + 1 <= record_depth:
                                child_path = os.path.normcase(os.path.abspath(entry.path))
                                items[child_path] = ScanItem(
                                    path=child_path,
                                    parent_path=os.path.normcase(
                                        os.path.abspath(directory)
                                    ),
                                    name=entry.name,
                                    kind="directory",
                                    size_bytes=child_size,
                                    file_count=child_files,
                                    depth=depth + 1,
                                    modified_at=child_modified,
                                )
                        elif entry.is_file(follow_symlinks=False):
                            size = entry_stat.st_size
                            modified = entry_stat.st_mtime
                            file_path = os.path.normcase(os.path.abspath(entry.path))
                            item = ScanItem(
                                path=file_path,
                                parent_path=os.path.normcase(
                                    os.path.abspath(directory)
                                ),
                                name=entry.name,
                                kind="file",
                                size_bytes=size,
                                file_count=1,
                                depth=depth + 1,
                                modified_at=modified,
                            )
                            if depth + 1 <= record_depth:
                                items[file_path] = item
                            remember_file(item)
                            total_size += size
                            file_count += 1
                            direct_file_bytes += size
                            direct_file_count += 1
                            latest_modified = max(latest_modified, modified)
                            if navigation_file_slots:
                                navigation_entry = (
                                    size,
                                    next(navigation_sequence),
                                    entry.name,
                                    modified,
                                )
                                if len(navigation_files) < navigation_file_slots:
                                    heapq.heappush(
                                        navigation_files, navigation_entry
                                    )
                                elif size > navigation_files[0][0]:
                                    heapq.heapreplace(
                                        navigation_files, navigation_entry
                                    )
                            counters["bytes"] += size
                            counters["files"] += 1
                            counters["since_progress"] += 1
                            report(entry.path)
                    except ScanCancelled:
                        raise
                    except (OSError, PermissionError):
                        counters["errors"] += 1
                        error_count += 1
        except ScanCancelled:
            raise
        except (OSError, PermissionError):
            counters["errors"] += 1
            error_count += 1

        visible_files = [
            NavigationItem(
                name=name,
                kind="file",
                size_bytes=size,
                file_count=1,
                modified_at=modified,
            )
            for size, _, name, modified in sorted(
                navigation_files, reverse=True
            )
        ]
        commit_navigation_slots(navigation_file_slots, len(visible_files))
        navigation_nodes[normalized_directory] = NavigationNode(
            total_bytes=total_size,
            file_count=file_count,
            directory_count=directory_count,
            error_count=error_count,
            modified_at=latest_modified,
            direct_file_bytes=direct_file_bytes,
            direct_file_count=direct_file_count,
            children=tuple(directory_children + visible_files),
        )
        return (
            total_size,
            file_count,
            directory_count,
            error_count,
            latest_modified,
        )

    (
        total_bytes,
        total_files,
        total_directories,
        total_errors,
        root_modified,
    ) = walk(root, 0)
    for _, _, item in top_files:
        items.setdefault(item.path, item)

    items[root] = ScanItem(
        path=root,
        parent_path=os.path.dirname(root),
        name=os.path.basename(root.rstrip("\\/")) or root,
        kind="directory",
        size_bytes=total_bytes,
        file_count=total_files,
        depth=0,
        modified_at=root_modified,
    )
    report(root, force=True)
    finished_at = datetime.now()
    skeleton = enforce_skeleton_budget(
        DirectorySkeleton(
            root_path=root,
            started_at=started_at,
            finished_at=finished_at,
            nodes=navigation_nodes,
        ),
        budget_bytes=navigation_memory_budget,
    )
    return ScanResult(
        root_path=root,
        started_at=started_at,
        finished_at=finished_at,
        total_bytes=total_bytes,
        file_count=total_files,
        directory_count=total_directories,
        error_count=total_errors,
        items=sorted(items.values(), key=lambda item: item.path),
        skeleton=skeleton,
    )
