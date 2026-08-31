from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .control_protocol import ControlError


SAFETY_NOTICE = (
    "仅供建议，请由用户在 Windows 或原应用中自行操作。"
    "本程序不会移动或修改文件。"
    "本视图不能证明文件未被占用，请先关闭相关应用；"
    "目标盘可用空间会随系统活动变化。"
)

_APP_MANAGED_EXTENSIONS = {
    ".appx",
    ".cab",
    ".db",
    ".dll",
    ".exe",
    ".hiberfil",
    ".msi",
    ".msix",
    ".sqlite",
    ".sqlite3",
    ".sys",
    ".vdi",
    ".vhd",
    ".vhdx",
    ".vmdk",
}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_OFFLINE = getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x1000)
_RECALL_ON_OPEN = 0x40000
_RECALL_ON_DATA_ACCESS = 0x400000


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _volume(path: str) -> str:
    drive, _tail = os.path.splitdrive(_normalized(path))
    return drive.casefold()


def _system_directories(active_data_directory: str | Path | None) -> tuple[str, ...]:
    directories = []
    for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(variable)
        if value:
            directories.append(_normalized(value))
    if active_data_directory is not None:
        directories.append(_normalized(active_data_directory))
    return tuple(dict.fromkeys(directories))


def build_migration_advice(
    items: Iterable[Any],
    target_path: str | Path,
    *,
    active_data_directory: str | Path | None = None,
    extension: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    limit: int = 200,
    inspection_limit: int = 1_000,
    disk_usage_reader: Callable[[str], Any] = shutil.disk_usage,
    stat_reader: Callable[..., os.stat_result] = os.stat,
) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ControlError("invalid_args", "limit 必须是正整数")
    if (
        not isinstance(inspection_limit, int)
        or isinstance(inspection_limit, bool)
        or inspection_limit < limit
    ):
        raise ControlError("invalid_args", "inspection_limit 不能小于 limit")
    normalized_extension = None
    if extension is not None:
        normalized_extension = extension.strip().lower()
        if not normalized_extension:
            raise ControlError("invalid_args", "extension 不能为空")
        if not normalized_extension.startswith("."):
            normalized_extension = "." + normalized_extension
        if any(character in normalized_extension for character in "*?%_"):
            raise ControlError("invalid_args", "extension 不能包含通配符")
    if min_size is not None and min_size < 0:
        raise ControlError("invalid_args", "min_size 不能为负数")
    if max_size is not None and max_size < 0:
        raise ControlError("invalid_args", "max_size 不能为负数")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise ControlError("invalid_args", "min_size 不能大于 max_size")
    target = _normalized(target_path)
    try:
        usage = disk_usage_reader(target)
    except OSError as error:
        raise ControlError("target_unavailable", "无法读取目标盘空间") from error
    target_volume = _volume(target)
    system_directories = _system_directories(active_data_directory)
    recorded_items = list(items)
    recorded_files = sorted(
        (
            item
            for item in recorded_items
            if _value(item, "kind") == "file"
            and (
                normalized_extension is None
                or str(_value(item, "path", "")).lower().endswith(
                    normalized_extension
                )
            )
            and (min_size is None or int(_value(item, "size_bytes", 0)) >= min_size)
            and (max_size is None or int(_value(item, "size_bytes", 0)) <= max_size)
        ),
        key=lambda item: (-int(_value(item, "size_bytes", 0)), _value(item, "path", "")),
    )
    inspected = recorded_files[:inspection_limit]
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in inspected:
        path = _normalized(str(_value(item, "path", "")))
        logical_size = int(_value(item, "size_bytes", 0))
        modified_at = float(_value(item, "modified_at", 0.0))
        extension = Path(path).suffix.lower()
        source_volume = _volume(path)
        reasons: list[str] = []
        parent = _normalized(os.path.dirname(path))
        source_root = source_volume + "\\" if source_volume else ""
        if source_root and parent == _normalized(source_root):
            reasons.append("system_root")
        if any(_path_is_within(path, directory) for directory in system_directories):
            reasons.append("system_or_app_data")
        if extension in _APP_MANAGED_EXTENSIONS:
            reasons.append("app_managed_type")
        if source_volume and target_volume and source_volume == target_volume:
            reasons.append("target_same_volume")

        link_count = _value(item, "link_count")
        file_id = _value(item, "file_id")
        if file_id is None:
            file_id = _value(item, "file_id_hex")
        measurement_state = _value(item, "measurement_state", "legacy")
        is_unique_owner = _value(item, "is_unique_owner")
        if link_count is not None and int(link_count) > 1:
            reasons.append("hard_link")
        if (
            measurement_state != "exact"
            or file_id is None
            or link_count is None
            or is_unique_owner is not True
        ):
            reasons.append("identity_incomplete")

        try:
            current = stat_reader(path, follow_symlinks=False)
        except FileNotFoundError:
            reasons.append("missing")
            current = None
        except PermissionError:
            reasons.append("permission_denied")
            current = None
        except OSError:
            reasons.append("metadata_unavailable")
            current = None
        if current is not None:
            attributes = int(getattr(current, "st_file_attributes", 0))
            if attributes & _REPARSE_POINT:
                reasons.append("reparse_point")
            if attributes & (_OFFLINE | _RECALL_ON_OPEN | _RECALL_ON_DATA_ACCESS):
                reasons.append("cloud_or_offline")
            current_mode = getattr(current, "st_mode", None)
            if current_mode is not None and not stat.S_ISREG(current_mode):
                reasons.append("not_regular_file")
            if int(current.st_size) != logical_size or abs(
                float(current.st_mtime) - modified_at
            ) > 0.001:
                reasons.append("snapshot_mismatch")

        base = {
            "path": path,
            "logical_size_bytes": logical_size,
            "allocated_size_bytes": _value(item, "allocated_size_bytes"),
            "unique_allocated_size_bytes": _value(
                item, "unique_allocated_size_bytes"
            ),
            "modified_at": modified_at,
            "extension": extension,
            "source_volume": source_volume,
            "target_path": target,
        }
        if reasons:
            excluded.append({**base, "reason_codes": list(dict.fromkeys(reasons))})
            continue
        unique_size = _value(item, "unique_allocated_size_bytes")
        if unique_size is not None:
            estimated_size = int(unique_size)
            estimate_basis = "unique_allocated_size"
        else:
            estimated_size = logical_size
            estimate_basis = "logical_size_conservative"
        candidates.append(
            {
                **base,
                "estimated_size_bytes": estimated_size,
                "estimate_basis": estimate_basis,
                "reason_code": "recorded_user_file",
            }
        )
    for item in recorded_items:
        if _value(item, "kind") == "file":
            continue
        path = _normalized(str(_value(item, "path", "")))
        excluded.append(
            {
                "path": path,
                "logical_size_bytes": int(_value(item, "size_bytes", 0)),
                "allocated_size_bytes": _value(item, "allocated_size_bytes"),
                "unique_allocated_size_bytes": _value(
                    item, "unique_allocated_size_bytes"
                ),
                "modified_at": float(_value(item, "modified_at", 0.0)),
                "extension": "",
                "source_volume": _volume(path),
                "target_path": target,
                "reason_codes": ["directory"],
            }
        )
    selected_candidates = candidates[:limit]
    estimated_total = sum(
        item["estimated_size_bytes"] for item in selected_candidates
    )
    free_bytes = int(usage.free)
    return {
        "notice": SAFETY_NOTICE,
        "target": {
            "path": target,
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": free_bytes,
            "estimated_remaining_bytes": free_bytes - estimated_total,
            "space_sufficient": free_bytes >= estimated_total,
            "space_is_point_in_time": True,
        },
        "candidates": selected_candidates,
        "excluded": excluded[:limit],
        "estimated_total_bytes": estimated_total,
        "recorded_item_count": len(recorded_items),
        "recorded_file_count": len(recorded_files),
        "inspected_file_count": len(inspected),
        "candidate_count": len(candidates),
        "excluded_count": len(excluded),
        "truncated": len(recorded_files) > len(inspected) or len(candidates) > limit,
        "coverage": "仅使用已记录文件明细；未额外扫描源盘",
    }
