from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any


def canonical_path_sort_key(path: str) -> tuple[str, str]:
    normalized = os.path.normcase(os.path.abspath(path)).replace("/", "\\")
    return normalized.casefold(), normalized


def _group_items(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, bytes], dict[str, Any]]:
    groups: dict[tuple[str, bytes], dict[str, Any]] = {}
    for row in rows:
        if (
            row["kind"] != "file"
            or row["measurement_state"] != "exact"
            or row["volume_serial_hex"] is None
            or row["file_id"] is None
        ):
            continue
        key = (str(row["volume_serial_hex"]), bytes(row["file_id"]))
        group = groups.setdefault(
            key,
            {
                "paths": [],
                "owner_paths": [],
                "allocated_sizes": set(),
                "link_counts": [],
            },
        )
        path = str(row["path"])
        group["paths"].append(path)
        if row["is_unique_owner"]:
            group["owner_paths"].append(path)
        if row["allocated_size_bytes"] is not None:
            group["allocated_sizes"].add(int(row["allocated_size_bytes"]))
        if row["link_count"] is not None:
            group["link_counts"].append(int(row["link_count"]))
    for group in groups.values():
        group["paths"] = tuple(
            sorted(group["paths"], key=canonical_path_sort_key)
        )
        group["owner_paths"] = tuple(
            sorted(group["owner_paths"], key=canonical_path_sort_key)
        )
    return groups


def compare_recorded_accounting(
    new_snapshot: Mapping[str, Any],
    old_snapshot: Mapping[str, Any],
    new_rows: Iterable[Mapping[str, Any]],
    old_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare exact totals and only attribute identities recorded on both sides."""

    base = {
        "new_snapshot_id": int(new_snapshot["id"]),
        "old_snapshot_id": int(old_snapshot["id"]),
        "new_measurement_state": new_snapshot["measurement_state"],
        "old_measurement_state": old_snapshot["measurement_state"],
        "items": [],
        "unresolved_items": [],
    }
    if os.path.normcase(str(new_snapshot["root_path"])) != os.path.normcase(
        str(old_snapshot["root_path"])
    ):
        return {
            **base,
            "available": False,
            "reason": "root_path_mismatch",
        }
    if (
        new_snapshot["measurement_state"] != "exact"
        or old_snapshot["measurement_state"] != "exact"
        or new_snapshot["allocated_total_bytes"] is None
        or old_snapshot["allocated_total_bytes"] is None
        or new_snapshot["unique_allocated_total_bytes"] is None
        or old_snapshot["unique_allocated_total_bytes"] is None
    ):
        return {
            **base,
            "available": False,
            "reason": "measurement_coverage_incomplete",
        }

    new_groups = _group_items(new_rows)
    old_groups = _group_items(old_rows)
    verified_items: list[dict[str, Any]] = []
    unresolved_items: list[dict[str, Any]] = []
    for volume_serial_hex, file_id in sorted(
        new_groups.keys() | old_groups.keys(),
        key=lambda key: (key[0], key[1]),
    ):
        key = (volume_serial_hex, file_id)
        new_group = new_groups.get(key)
        old_group = old_groups.get(key)
        identity = {
            "volume_serial_hex": volume_serial_hex,
            "file_id_hex": file_id.hex(),
        }
        if new_group is None or old_group is None:
            present = new_group or old_group
            assert present is not None
            unresolved_items.append(
                {
                    **identity,
                    "reason": "identity_not_recorded_in_both_snapshots",
                    "old_paths": old_group["paths"] if old_group else (),
                    "new_paths": new_group["paths"] if new_group else (),
                }
            )
            continue
        if (
            len(new_group["allocated_sizes"]) != 1
            or len(old_group["allocated_sizes"]) != 1
        ):
            unresolved_items.append(
                {
                    **identity,
                    "reason": "recorded_allocation_inconsistent",
                    "old_paths": old_group["paths"],
                    "new_paths": new_group["paths"],
                }
            )
            continue
        old_allocated = next(iter(old_group["allocated_sizes"]))
        new_allocated = next(iter(new_group["allocated_sizes"]))
        old_owner = (
            old_group["owner_paths"][0]
            if old_group["owner_paths"]
            else None
        )
        new_owner = (
            new_group["owner_paths"][0]
            if new_group["owner_paths"]
            else None
        )
        change = new_allocated - old_allocated
        paths_changed = old_group["paths"] != new_group["paths"]
        ownership_changed = old_owner != new_owner
        if change or paths_changed or ownership_changed:
            verified_items.append(
                {
                    **identity,
                    "old_allocated_size_bytes": old_allocated,
                    "new_allocated_size_bytes": new_allocated,
                    "change_bytes": change,
                    "old_paths": old_group["paths"],
                    "new_paths": new_group["paths"],
                    "old_owner_path": old_owner,
                    "new_owner_path": new_owner,
                    "paths_changed": paths_changed,
                    "ownership_changed": ownership_changed,
                    "change_kind": (
                        "content_size_changed"
                        if change
                        else "accounting_path_changed"
                    ),
                }
            )

    verified_items.sort(
        key=lambda item: (
            -abs(item["change_bytes"]),
            canonical_path_sort_key(
                item["new_owner_path"]
                or item["old_owner_path"]
                or (item["new_paths"] or item["old_paths"])[0]
            ),
        )
    )
    unique_change = int(new_snapshot["unique_allocated_total_bytes"]) - int(
        old_snapshot["unique_allocated_total_bytes"]
    )
    verified_change = sum(item["change_bytes"] for item in verified_items)
    return {
        **base,
        "available": True,
        "reason": None,
        "logical_total_change_bytes": (
            int(new_snapshot["total_bytes"])
            - int(old_snapshot["total_bytes"])
        ),
        "allocated_total_change_bytes": (
            int(new_snapshot["allocated_total_bytes"])
            - int(old_snapshot["allocated_total_bytes"])
        ),
        "unique_allocated_total_change_bytes": unique_change,
        "verified_item_change_bytes": verified_change,
        "unattributed_unique_change_bytes": unique_change - verified_change,
        "recorded_identity_count_old": len(old_groups),
        "recorded_identity_count_new": len(new_groups),
        "items": verified_items,
        "unresolved_items": unresolved_items,
    }
