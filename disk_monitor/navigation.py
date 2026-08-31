from __future__ import annotations

import os
import sys
from dataclasses import replace

from .models import (
    DirectorySkeleton,
    NavigationItem,
    ScanItem,
    ScanResult,
)


NAVIGATION_MEMORY_BUDGET_BYTES = 50 * 1024 * 1024
OTHER_FILES_LABEL = "其他文件"


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def path_is_within(path: str, parent: str) -> bool:
    try:
        normalized_path = normalize_path(path)
        normalized_parent = normalize_path(parent)
        return os.path.commonpath((normalized_path, normalized_parent)) == normalized_parent
    except ValueError:
        return False


def estimate_skeleton_bytes(skeleton: DirectorySkeleton) -> int:
    total = sys.getsizeof(skeleton) + sys.getsizeof(skeleton.nodes)
    for path, node in skeleton.nodes.items():
        total += sys.getsizeof(path) + sys.getsizeof(node) + sys.getsizeof(node.children)
        for child in node.children:
            total += sys.getsizeof(child) + sys.getsizeof(child.name)
            total += sys.getsizeof(child.size_bytes) + sys.getsizeof(child.file_count)
            total += sys.getsizeof(child.volume_serial_hex)
            total += sys.getsizeof(child.file_id)
    return total


def enforce_skeleton_budget(
    skeleton: DirectorySkeleton,
    *,
    budget_bytes: int = NAVIGATION_MEMORY_BUDGET_BYTES,
) -> DirectorySkeleton:
    if budget_bytes < 1:
        raise ValueError("budget_bytes 必须至少为 1")
    estimated = estimate_skeleton_bytes(skeleton)
    if estimated <= budget_bytes:
        skeleton.estimated_bytes = estimated
        return skeleton

    compacted_nodes = {
        path: replace(
            node,
            children=tuple(
                child for child in node.children if child.kind == "directory"
            ),
        )
        for path, node in skeleton.nodes.items()
    }
    compacted = DirectorySkeleton(
        root_path=skeleton.root_path,
        started_at=skeleton.started_at,
        finished_at=skeleton.finished_at,
        nodes=compacted_nodes,
        degraded=True,
    )
    compacted.estimated_bytes = estimate_skeleton_bytes(compacted)
    return compacted


def materialize_navigation_result(
    skeleton: DirectorySkeleton, path: str
) -> ScanResult | None:
    normalized = normalize_path(path)
    node = skeleton.nodes.get(normalized)
    if node is None:
        return None

    items: list[ScanItem] = []
    visible_file_bytes = 0
    visible_file_count = 0
    for child in node.children:
        child_path = normalize_path(os.path.join(normalized, child.name))
        items.append(
            ScanItem(
                path=child_path,
                parent_path=normalized,
                name=child.name,
                kind=child.kind,
                size_bytes=child.size_bytes,
                file_count=child.file_count,
                depth=1,
                modified_at=child.modified_at,
                allocated_size_bytes=child.allocated_size_bytes,
                unique_allocated_size_bytes=(
                    child.unique_allocated_size_bytes
                ),
                volume_serial_hex=child.volume_serial_hex,
                file_id=child.file_id,
                link_count=child.link_count,
                is_unique_owner=child.is_unique_owner,
                measurement_state=child.measurement_state,
            )
        )
        if child.kind == "file":
            visible_file_bytes += child.size_bytes
            visible_file_count += child.file_count

    other_file_count = max(node.direct_file_count - visible_file_count, 0)
    other_file_bytes = max(node.direct_file_bytes - visible_file_bytes, 0)
    if other_file_count or other_file_bytes:
        items.append(
            ScanItem(
                path=os.path.join(normalized, "<other-files>"),
                parent_path=normalized,
                name=f"{OTHER_FILES_LABEL}（{other_file_count:,} 个）",
                kind="aggregate",
                size_bytes=other_file_bytes,
                file_count=other_file_count,
                depth=1,
            )
        )

    items.append(
        ScanItem(
            path=normalized,
            parent_path=os.path.dirname(normalized),
            name=os.path.basename(normalized.rstrip("\\/")) or normalized,
            kind="directory",
            size_bytes=node.total_bytes,
            file_count=node.file_count,
            depth=0,
            modified_at=node.modified_at,
            allocated_size_bytes=node.allocated_size_bytes,
            unique_allocated_size_bytes=node.unique_allocated_size_bytes,
            measurement_state=node.measurement_state,
        )
    )
    return ScanResult(
        root_path=normalized,
        started_at=skeleton.started_at,
        finished_at=skeleton.finished_at,
        total_bytes=node.total_bytes,
        file_count=node.file_count,
        directory_count=node.directory_count,
        error_count=node.error_count,
        items=items,
        skeleton=skeleton,
        allocated_total_bytes=node.allocated_size_bytes,
        unique_allocated_total_bytes=node.unique_allocated_size_bytes,
        measured_allocated_bytes=node.measured_allocated_bytes,
        measured_unique_allocated_bytes=(
            node.measured_unique_allocated_bytes
        ),
        eligible_file_count=node.eligible_file_count,
        allocation_measured_file_count=(
            node.allocation_measured_file_count
        ),
        identity_measured_file_count=node.identity_measured_file_count,
        metadata_error_count=node.metadata_error_count,
        measurement_state=node.measurement_state,
    )


def merge_directory_skeleton(
    base: DirectorySkeleton,
    update: DirectorySkeleton,
    *,
    budget_bytes: int = NAVIGATION_MEMORY_BUDGET_BYTES,
) -> DirectorySkeleton:
    base_root = normalize_path(base.root_path)
    update_root = normalize_path(update.root_path)
    if base_root == update_root:
        return enforce_skeleton_budget(update, budget_bytes=budget_bytes)
    if not path_is_within(update_root, base_root):
        return base

    old_root_node = base.nodes.get(update_root)
    new_root_node = update.nodes.get(update_root)
    if new_root_node is None:
        return base

    nodes = {
        path: node
        for path, node in base.nodes.items()
        if not path_is_within(path, update_root)
    }
    nodes.update(update.nodes)

    old_total = old_root_node.total_bytes if old_root_node else 0
    old_files = old_root_node.file_count if old_root_node else 0
    old_directories = old_root_node.directory_count if old_root_node else 0
    old_errors = old_root_node.error_count if old_root_node else 0
    delta_total = new_root_node.total_bytes - old_total
    delta_files = new_root_node.file_count - old_files
    delta_directories = new_root_node.directory_count - old_directories
    delta_errors = new_root_node.error_count - old_errors

    child_path = update_root
    parent_path = normalize_path(os.path.dirname(child_path))
    while path_is_within(parent_path, base_root):
        parent_node = nodes.get(parent_path)
        child_node = nodes.get(child_path)
        if parent_node is None or child_node is None:
            break
        child_name = os.path.basename(child_path.rstrip("\\/")) or child_path
        replacement = NavigationItem(
            name=child_name,
            kind="directory",
            size_bytes=child_node.total_bytes,
            file_count=child_node.file_count,
            modified_at=child_node.modified_at,
            allocated_size_bytes=child_node.allocated_size_bytes,
            unique_allocated_size_bytes=(
                child_node.unique_allocated_size_bytes
            ),
            measurement_state=child_node.measurement_state,
        )
        children = list(parent_node.children)
        for index, item in enumerate(children):
            if item.kind == "directory" and item.name == child_name:
                children[index] = replacement
                break
        else:
            children.append(replacement)
        nodes[parent_path] = replace(
            parent_node,
            total_bytes=max(parent_node.total_bytes + delta_total, 0),
            file_count=max(parent_node.file_count + delta_files, 0),
            directory_count=max(
                parent_node.directory_count + delta_directories, 1
            ),
            error_count=max(parent_node.error_count + delta_errors, 0),
            modified_at=max(parent_node.modified_at, child_node.modified_at),
            children=tuple(children),
            allocated_size_bytes=None,
            unique_allocated_size_bytes=None,
            measured_allocated_bytes=0,
            measured_unique_allocated_bytes=0,
            eligible_file_count=0,
            allocation_measured_file_count=0,
            identity_measured_file_count=0,
            metadata_error_count=0,
            measurement_state=(
                "legacy"
                if parent_node.measurement_state == "legacy"
                and child_node.measurement_state == "legacy"
                else "partial"
            ),
        )
        if parent_path == base_root:
            break
        child_path = parent_path
        next_parent = normalize_path(os.path.dirname(parent_path))
        if next_parent == parent_path:
            break
        parent_path = next_parent

    merged = DirectorySkeleton(
        root_path=base_root,
        started_at=base.started_at,
        finished_at=update.finished_at,
        nodes=nodes,
        degraded=base.degraded or update.degraded,
    )
    return enforce_skeleton_budget(merged, budget_bytes=budget_bytes)
