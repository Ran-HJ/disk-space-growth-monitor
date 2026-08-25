from __future__ import annotations

import heapq
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass
class _DirectoryFrame:
    directory: str
    depth: int
    name: str
    parent: _DirectoryFrame | None = None
    total_size: int = 0
    file_count: int = 0
    directory_count: int = 1
    error_count: int = 0
    latest_modified: float = 0.0
    direct_file_bytes: int = 0
    direct_file_count: int = 0
    directory_children: list[NavigationItem] = field(default_factory=list)
    navigation_file_slots: int = 0
    navigation_files: list[tuple[int, int, str, float]] = field(
        default_factory=list
    )


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

    root_frame = _DirectoryFrame(
        directory=root,
        depth=0,
        name=os.path.basename(root.rstrip("\\/")) or root,
    )
    stack: list[tuple[str, _DirectoryFrame]] = [("enter", root_frame)]
    while stack:
        if cancel.is_set():
            raise ScanCancelled
        action, frame = stack.pop()
        if action == "enter":
            frame.navigation_file_slots = reserve_navigation_slots()
            counters["directories"] += 1
            stack.append(("exit", frame))
            child_frames: list[_DirectoryFrame] = []
            try:
                with os.scandir(frame.directory) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise ScanCancelled
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                            if entry.is_symlink() or _is_reparse_point(entry_stat):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child_frames.append(
                                    _DirectoryFrame(
                                        directory=os.path.normcase(
                                            os.path.abspath(entry.path)
                                        ),
                                        depth=frame.depth + 1,
                                        name=entry.name,
                                        parent=frame,
                                    )
                                )
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            size = entry_stat.st_size
                            modified = entry_stat.st_mtime
                            file_path = os.path.normcase(os.path.abspath(entry.path))
                            item = ScanItem(
                                path=file_path,
                                parent_path=frame.directory,
                                name=entry.name,
                                kind="file",
                                size_bytes=size,
                                file_count=1,
                                depth=frame.depth + 1,
                                modified_at=modified,
                            )
                            if frame.depth + 1 <= record_depth:
                                items[file_path] = item
                            remember_file(item)
                            frame.total_size += size
                            frame.file_count += 1
                            frame.direct_file_bytes += size
                            frame.direct_file_count += 1
                            frame.latest_modified = max(
                                frame.latest_modified, modified
                            )
                            if frame.navigation_file_slots:
                                navigation_entry = (
                                    size,
                                    next(navigation_sequence),
                                    entry.name,
                                    modified,
                                )
                                if (
                                    len(frame.navigation_files)
                                    < frame.navigation_file_slots
                                ):
                                    heapq.heappush(
                                        frame.navigation_files,
                                        navigation_entry,
                                    )
                                elif size > frame.navigation_files[0][0]:
                                    heapq.heapreplace(
                                        frame.navigation_files,
                                        navigation_entry,
                                    )
                            counters["bytes"] += size
                            counters["files"] += 1
                            counters["since_progress"] += 1
                            report(entry.path)
                        except OSError:
                            counters["errors"] += 1
                            frame.error_count += 1
            except ScanCancelled:
                raise
            except OSError:
                counters["errors"] += 1
                frame.error_count += 1
            for child_frame in reversed(child_frames):
                stack.append(("enter", child_frame))
            continue

        visible_files = [
            NavigationItem(
                name=name,
                kind="file",
                size_bytes=size,
                file_count=1,
                modified_at=modified,
            )
            for size, _, name, modified in sorted(
                frame.navigation_files, reverse=True
            )
        ]
        commit_navigation_slots(frame.navigation_file_slots, len(visible_files))
        navigation_nodes[frame.directory] = NavigationNode(
            total_bytes=frame.total_size,
            file_count=frame.file_count,
            directory_count=frame.directory_count,
            error_count=frame.error_count,
            modified_at=frame.latest_modified,
            direct_file_bytes=frame.direct_file_bytes,
            direct_file_count=frame.direct_file_count,
            children=tuple(frame.directory_children + visible_files),
        )
        if frame.parent is not None:
            parent = frame.parent
            parent.total_size += frame.total_size
            parent.file_count += frame.file_count
            parent.directory_count += frame.directory_count
            parent.error_count += frame.error_count
            parent.latest_modified = max(
                parent.latest_modified, frame.latest_modified
            )
            parent.directory_children.append(
                NavigationItem(
                    name=frame.name,
                    kind="directory",
                    size_bytes=frame.total_size,
                    file_count=frame.file_count,
                    modified_at=frame.latest_modified,
                )
            )
            if frame.depth <= record_depth:
                items[frame.directory] = ScanItem(
                    path=frame.directory,
                    parent_path=parent.directory,
                    name=frame.name,
                    kind="directory",
                    size_bytes=frame.total_size,
                    file_count=frame.file_count,
                    depth=frame.depth,
                    modified_at=frame.latest_modified,
                )

    total_bytes = root_frame.total_size
    total_files = root_frame.file_count
    total_directories = root_frame.directory_count
    total_errors = root_frame.error_count
    root_modified = root_frame.latest_modified
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
