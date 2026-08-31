from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any


SCAN_CONFIG_VERSION = 1


def canonical_scan_config_json(
    root_path: str,
    *,
    exclude_rules: Sequence[str] = (),
    collect_file_space: bool,
    record_depth: int,
    top_file_limit: int,
    navigation_file_limit: int,
    navigation_total_file_limit: int,
    navigation_memory_budget: int,
) -> str:
    """返回可直接比较的扫描配置原文，不生成额外指纹或哈希。"""

    payload = {
        "collect_file_space": bool(collect_file_space),
        "exclude_rules": list(exclude_rules),
        "navigation_file_limit": navigation_file_limit,
        "navigation_memory_budget": navigation_memory_budget,
        "navigation_total_file_limit": navigation_total_file_limit,
        "record_depth": record_depth,
        "root_path": os.path.normcase(os.path.abspath(root_path)),
        "top_file_limit": top_file_limit,
        "version": SCAN_CONFIG_VERSION,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compare_scan_configs(
    new_version: int,
    new_json: str | None,
    old_version: int,
    old_json: str | None,
) -> dict[str, Any]:
    if new_version <= 0 or old_version <= 0 or not new_json or not old_json:
        return {
            "status": "unknown",
            "differences": ["旧快照配置未知"],
        }
    if new_version == old_version and new_json == old_json:
        return {"status": "compatible", "differences": []}

    differences: list[str] = []
    if new_version != old_version:
        differences.append("scan_config_version")
    try:
        new_config = json.loads(new_json)
        old_config = json.loads(old_json)
    except (TypeError, json.JSONDecodeError):
        differences.append("scan_config_json")
    else:
        if not isinstance(new_config, dict) or not isinstance(old_config, dict):
            differences.append("scan_config_json")
        else:
            for key in sorted(set(new_config) | set(old_config)):
                if new_config.get(key) != old_config.get(key):
                    differences.append(key)
    if not differences:
        differences.append("scan_config_json")
    return {"status": "mismatch", "differences": differences}
