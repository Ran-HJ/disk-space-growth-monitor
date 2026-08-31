from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .automation import AutoModeConfig, normalize_process_names
from .exclusions import compile_exclusion_rules


@dataclass(frozen=True)
class SettingsSubmission:
    """Validated values collected by the settings dialog before persistence."""

    close_behavior: str
    run_mode: str
    autostart_enabled: bool
    collect_file_space: bool
    exclude_rules: tuple[str, ...]
    auto_mode_config: AutoModeConfig


def _key_for_label(
    labels: Mapping[str, str], selected_label: str, field_name: str
) -> str:
    for key, label in labels.items():
        if label == selected_label:
            return key
    raise ValueError(f"{field_name}选项无效")


def validate_settings_submission(
    *,
    close_behavior_label: str,
    run_mode_label: str,
    autostart_enabled: bool,
    collect_file_space: bool,
    exclude_rules_text: str,
    auto_enabled: bool,
    process_names_text: str,
    memory_pressure_enabled: bool,
    high_percent_text: str,
    low_percent_text: str,
    resume_rescan_label: str,
    session_root_path: str,
    close_behavior_labels: Mapping[str, str],
    run_mode_labels: Mapping[str, str],
    auto_rescan_labels: Mapping[str, str],
) -> SettingsSubmission:
    """Validate dialog text without mutating the app, storage, or Windows settings."""

    exclude_rules = tuple(
        line.strip() for line in exclude_rules_text.splitlines() if line.strip()
    )
    compile_exclusion_rules(session_root_path, list(exclude_rules))
    auto_mode_config = AutoModeConfig(
        enabled=auto_enabled,
        process_names=normalize_process_names(process_names_text),
        memory_pressure_enabled=memory_pressure_enabled,
        high_percent=int(high_percent_text.strip()),
        low_percent=int(low_percent_text.strip()),
        resume_rescan=_key_for_label(
            auto_rescan_labels, resume_rescan_label, "恢复补扫策略"
        ),
    ).validate()
    return SettingsSubmission(
        close_behavior=_key_for_label(
            close_behavior_labels, close_behavior_label, "关闭方式"
        ),
        run_mode=_key_for_label(run_mode_labels, run_mode_label, "运行模式"),
        autostart_enabled=autostart_enabled,
        collect_file_space=collect_file_space,
        exclude_rules=exclude_rules,
        auto_mode_config=auto_mode_config,
    )
