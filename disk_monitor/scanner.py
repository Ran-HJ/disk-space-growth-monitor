from __future__ import annotations

import heapq
import os
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import count

from .accounting import canonical_path_sort_key
from .exclusions import compile_exclusion_rules
from .models import (
    DirectorySkeleton,
    NavigationItem,
    NavigationNode,
    ScanItem,
    ScanProgress,
    ScanResult,
)
from .navigation import NAVIGATION_MEMORY_BUDGET_BYTES, enforce_skeleton_budget
from .scan_config import SCAN_CONFIG_VERSION, canonical_scan_config_json
from .windows_file_info import FileSpaceInfo, read_file_space_info


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
    navigation_files: list[tuple[int, int, ScanItem]] = field(default_factory=list)
    measured_allocated_bytes: int = 0
    eligible_file_count: int = 0
    allocation_measured_file_count: int = 0
    identity_measured_file_count: int = 0
    metadata_error_count: int = 0


@dataclass
class _IdentityRecord:
    owner_path: str
    owner_sort_key: tuple[str, str]
    allocated_size_bytes: int
    inconsistent: bool = False
    conflict_paths: list[str] | None = None


@dataclass(frozen=True)
class _DirectoryAccounting:
    measured_allocated_bytes: int
    eligible_file_count: int
    allocation_measured_file_count: int
    identity_measured_file_count: int
    metadata_error_count: int
    scan_error_count: int


def _measurement_state(information: FileSpaceInfo | None) -> str:
    if information is None:
        return "legacy"
    if information.state == "exact":
        return "exact"
    if information.allocated_size_bytes is not None:
        return "partial"
    return "unavailable"


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
    collect_file_space: bool = False,
    exclude_rules: Sequence[str] = (),
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
    exclusions = compile_exclusion_rules(root, list(exclude_rules))

    started_at = datetime.now()
    cancel = cancel_event or threading.Event()
    items: dict[str, ScanItem] = {}
    top_files: list[tuple[int, int, ScanItem]] = []
    sequence = count()
    navigation_sequence = count()
    navigation_nodes: dict[str, NavigationNode] = {}
    directory_accounting: dict[str, _DirectoryAccounting] = {}
    identity_records: dict[tuple[str, bytes], _IdentityRecord] = {}
    navigation_reserved_slots = 0
    navigation_committed_slots = 0
    counters = {
        "bytes": 0,
        "files": 0,
        "directories": 0,
        "errors": 0,
        "excluded": 0,
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
                            entry_path = os.path.normcase(
                                os.path.abspath(entry.path)
                            )
                            if exclusions.matches(entry_path):
                                counters["excluded"] += 1
                                continue
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
                            file_path = entry_path
                            if collect_file_space:
                                space_information = read_file_space_info(
                                    file_path, expected_size_bytes=size
                                )
                                measurement_state = _measurement_state(
                                    space_information
                                )
                                allocated_size = None
                                if space_information.state in {
                                    "exact",
                                    "partial",
                                }:
                                    allocated_size = (
                                        space_information.allocated_size_bytes
                                    )
                                identity_key = (
                                    space_information.identity_key
                                    if space_information.state == "exact"
                                    else None
                                )
                            else:
                                space_information = None
                                measurement_state = "legacy"
                                allocated_size = None
                                identity_key = None
                            item = ScanItem(
                                path=file_path,
                                parent_path=frame.directory,
                                name=entry.name,
                                kind="file",
                                size_bytes=size,
                                file_count=1,
                                depth=frame.depth + 1,
                                modified_at=modified,
                                allocated_size_bytes=allocated_size,
                                volume_serial_hex=(
                                    space_information.volume_serial_hex
                                    if space_information is not None
                                    else None
                                ),
                                file_id=(
                                    space_information.file_id
                                    if space_information is not None
                                    else None
                                ),
                                link_count=(
                                    space_information.link_count
                                    if space_information is not None
                                    else None
                                ),
                                measurement_state=measurement_state,
                            )
                            if frame.depth + 1 <= record_depth:
                                items[file_path] = item
                            remember_file(item)
                            frame.total_size += size
                            frame.file_count += 1
                            frame.direct_file_bytes += size
                            frame.direct_file_count += 1
                            if collect_file_space:
                                frame.eligible_file_count += 1
                                if allocated_size is not None:
                                    frame.measured_allocated_bytes += allocated_size
                                    frame.allocation_measured_file_count += 1
                                if identity_key is None:
                                    frame.metadata_error_count += 1
                                else:
                                    frame.identity_measured_file_count += 1
                                    sort_key = canonical_path_sort_key(file_path)
                                    record = identity_records.get(identity_key)
                                    if record is None:
                                        identity_records[identity_key] = (
                                            _IdentityRecord(
                                                owner_path=file_path,
                                                owner_sort_key=sort_key,
                                                allocated_size_bytes=(
                                                    allocated_size or 0
                                                ),
                                            )
                                        )
                                    else:
                                        if (
                                            record.allocated_size_bytes
                                            != allocated_size
                                        ):
                                            record.inconsistent = True
                                            if record.conflict_paths is None:
                                                record.conflict_paths = [
                                                    record.owner_path
                                                ]
                                            record.conflict_paths.append(file_path)
                                        elif (
                                            record.inconsistent
                                            and record.conflict_paths is not None
                                        ):
                                            record.conflict_paths.append(file_path)
                                        if sort_key < record.owner_sort_key:
                                            record.owner_path = file_path
                                            record.owner_sort_key = sort_key
                            frame.latest_modified = max(
                                frame.latest_modified, modified
                            )
                            if frame.navigation_file_slots:
                                navigation_entry = (
                                    size,
                                    next(navigation_sequence),
                                    item,
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
                name=item.name,
                kind="file",
                size_bytes=item.size_bytes,
                file_count=1,
                modified_at=item.modified_at,
                allocated_size_bytes=item.allocated_size_bytes,
                volume_serial_hex=item.volume_serial_hex,
                file_id=item.file_id,
                link_count=item.link_count,
                measurement_state=item.measurement_state,
            )
            for _, _, item in sorted(frame.navigation_files, reverse=True)
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
        if collect_file_space:
            directory_accounting[frame.directory] = _DirectoryAccounting(
                measured_allocated_bytes=frame.measured_allocated_bytes,
                eligible_file_count=frame.eligible_file_count,
                allocation_measured_file_count=(
                    frame.allocation_measured_file_count
                ),
                identity_measured_file_count=(
                    frame.identity_measured_file_count
                ),
                metadata_error_count=frame.metadata_error_count,
                scan_error_count=frame.error_count,
            )
        if frame.parent is not None:
            parent = frame.parent
            parent.total_size += frame.total_size
            parent.file_count += frame.file_count
            parent.directory_count += frame.directory_count
            parent.error_count += frame.error_count
            if collect_file_space:
                parent.measured_allocated_bytes += (
                    frame.measured_allocated_bytes
                )
                parent.eligible_file_count += frame.eligible_file_count
                parent.allocation_measured_file_count += (
                    frame.allocation_measured_file_count
                )
                parent.identity_measured_file_count += (
                    frame.identity_measured_file_count
                )
                parent.metadata_error_count += frame.metadata_error_count
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

    unique_by_directory = {
        path: 0 for path in directory_accounting
    }
    conflict_directories: set[str] = set()
    identity_conflict_count = 0

    def ancestor_directories(file_path: str):
        directory = os.path.normcase(os.path.abspath(os.path.dirname(file_path)))
        while directory in directory_accounting:
            yield directory
            if directory == root:
                break
            parent = os.path.normcase(os.path.abspath(os.path.dirname(directory)))
            if parent == directory:
                break
            directory = parent

    measured_unique_allocated_bytes = 0
    for record in identity_records.values():
        if record.inconsistent:
            identity_conflict_count += 1
            for conflict_path in record.conflict_paths or [record.owner_path]:
                conflict_directories.update(ancestor_directories(conflict_path))
            continue
        measured_unique_allocated_bytes += record.allocated_size_bytes
        for directory in ancestor_directories(record.owner_path):
            unique_by_directory[directory] += record.allocated_size_bytes

    def directory_measurement(
        directory: str,
    ) -> tuple[int | None, int | None, str]:
        if not collect_file_space:
            return None, None, "legacy"
        accounting = directory_accounting[directory]
        allocation_complete = (
            accounting.scan_error_count == 0
            and accounting.allocation_measured_file_count
            == accounting.eligible_file_count
        )
        identity_complete = (
            allocation_complete
            and accounting.metadata_error_count == 0
            and accounting.identity_measured_file_count
            == accounting.eligible_file_count
            and directory not in conflict_directories
        )
        allocated = (
            accounting.measured_allocated_bytes
            if allocation_complete
            else None
        )
        unique_allocated = (
            unique_by_directory[directory]
            if identity_complete
            else None
        )
        if identity_complete:
            state = "exact"
        elif accounting.allocation_measured_file_count > 0:
            state = "partial"
        else:
            state = "unavailable"
        return allocated, unique_allocated, state

    def finalize_item(item: ScanItem) -> ScanItem:
        if item.kind == "directory":
            allocated, unique_allocated, state = directory_measurement(
                item.path
            )
            return replace(
                item,
                allocated_size_bytes=allocated,
                unique_allocated_size_bytes=unique_allocated,
                measurement_state=state,
            )
        if (
            item.kind != "file"
            or item.volume_serial_hex is None
            or item.file_id is None
            or item.measurement_state != "exact"
        ):
            return item
        record = identity_records[(item.volume_serial_hex, item.file_id)]
        if record.inconsistent:
            return replace(item, measurement_state="partial")
        is_owner = item.path == record.owner_path
        return replace(
            item,
            unique_allocated_size_bytes=(
                record.allocated_size_bytes if is_owner else 0
            ),
            is_unique_owner=is_owner,
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
    if collect_file_space:
        items = {
            path: finalize_item(item)
            for path, item in items.items()
        }
        finalized_navigation_nodes: dict[str, NavigationNode] = {}
        for directory, node in navigation_nodes.items():
            allocated, unique_allocated, state = directory_measurement(
                directory
            )
            children: list[NavigationItem] = []
            for child in node.children:
                child_path = os.path.normcase(
                    os.path.abspath(os.path.join(directory, child.name))
                )
                if child.kind == "directory":
                    child_allocated, child_unique, child_state = (
                        directory_measurement(child_path)
                    )
                    children.append(
                        replace(
                            child,
                            allocated_size_bytes=child_allocated,
                            unique_allocated_size_bytes=child_unique,
                            measurement_state=child_state,
                        )
                    )
                    continue
                if (
                    child.volume_serial_hex is None
                    or child.file_id is None
                    or child.measurement_state != "exact"
                ):
                    children.append(child)
                    continue
                record = identity_records[
                    (child.volume_serial_hex, child.file_id)
                ]
                if record.inconsistent:
                    children.append(
                        replace(child, measurement_state="partial")
                    )
                    continue
                is_owner = child_path == record.owner_path
                children.append(
                    replace(
                        child,
                        unique_allocated_size_bytes=(
                            record.allocated_size_bytes if is_owner else 0
                        ),
                        is_unique_owner=is_owner,
                    )
                )
            accounting = directory_accounting[directory]
            finalized_navigation_nodes[directory] = replace(
                node,
                children=tuple(children),
                allocated_size_bytes=allocated,
                unique_allocated_size_bytes=unique_allocated,
                measured_allocated_bytes=(
                    accounting.measured_allocated_bytes
                ),
                measured_unique_allocated_bytes=(
                    unique_by_directory[directory]
                ),
                eligible_file_count=accounting.eligible_file_count,
                allocation_measured_file_count=(
                    accounting.allocation_measured_file_count
                ),
                identity_measured_file_count=(
                    accounting.identity_measured_file_count
                ),
                metadata_error_count=(
                    accounting.metadata_error_count
                    + int(directory in conflict_directories)
                ),
                measurement_state=state,
            )
        navigation_nodes = finalized_navigation_nodes
        root_allocated, root_unique_allocated, root_measurement_state = (
            directory_measurement(root)
        )
        root_accounting = directory_accounting[root]
        measured_allocated_bytes = root_accounting.measured_allocated_bytes
        eligible_file_count = root_accounting.eligible_file_count
        allocation_measured_file_count = (
            root_accounting.allocation_measured_file_count
        )
        identity_measured_file_count = (
            root_accounting.identity_measured_file_count
        )
        metadata_error_count = (
            root_accounting.metadata_error_count + identity_conflict_count
        )
    else:
        root_allocated = None
        root_unique_allocated = None
        root_measurement_state = "legacy"
        measured_allocated_bytes = 0
        eligible_file_count = 0
        allocation_measured_file_count = 0
        identity_measured_file_count = 0
        metadata_error_count = 0
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
        allocated_total_bytes=root_allocated,
        unique_allocated_total_bytes=root_unique_allocated,
        measured_allocated_bytes=measured_allocated_bytes,
        measured_unique_allocated_bytes=measured_unique_allocated_bytes,
        eligible_file_count=eligible_file_count,
        allocation_measured_file_count=allocation_measured_file_count,
        identity_measured_file_count=identity_measured_file_count,
        metadata_error_count=metadata_error_count,
        measurement_state=root_measurement_state,
        scan_config_version=SCAN_CONFIG_VERSION,
        scan_config_json=canonical_scan_config_json(
            root,
            exclude_rules=exclusions.serialized_rules,
            collect_file_space=collect_file_space,
            record_depth=record_depth,
            top_file_limit=top_file_limit,
            navigation_file_limit=navigation_file_limit,
            navigation_total_file_limit=navigation_total_file_limit,
            navigation_memory_budget=navigation_memory_budget,
        ),
        excluded_rule_count=len(exclusions.rules),
        excluded_item_count=counters["excluded"],
    )
