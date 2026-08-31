from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from disk_monitor.settings_view import validate_settings_submission


CLOSE_BEHAVIOR_LABELS = {
    "ask": "每次询问",
    "full": "始终完整保存",
    "quick": "始终快速退出",
}
RUN_MODE_LABELS = {"full": "全功能模式", "low_memory": "低内存模式"}
AUTO_RESCAN_LABELS = {"later": "稍后手动补扫（推荐）", "now": "立即补扫"}


class SettingsViewTests(unittest.TestCase):
    def test_validation_normalizes_and_returns_a_complete_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = validate_settings_submission(
                close_behavior_label="始终完整保存",
                run_mode_label="低内存模式",
                autostart_enabled=True,
                collect_file_space=True,
                exclude_rules_text="*.tmp\ncache/**\n",
                auto_enabled=True,
                process_names_text="R5APEX; cs2.exe",
                memory_pressure_enabled=False,
                high_percent_text="90",
                low_percent_text="80",
                resume_rescan_label="立即补扫",
                session_root_path=temp_dir,
                close_behavior_labels=CLOSE_BEHAVIOR_LABELS,
                run_mode_labels=RUN_MODE_LABELS,
                auto_rescan_labels=AUTO_RESCAN_LABELS,
            )

        self.assertEqual(result.close_behavior, "full")
        self.assertEqual(result.run_mode, "low_memory")
        self.assertTrue(result.autostart_enabled)
        self.assertTrue(result.collect_file_space)
        self.assertEqual(result.exclude_rules, ("*.tmp", "cache/**"))
        self.assertEqual(
            result.auto_mode_config.process_names, ("r5apex.exe", "cs2.exe")
        )
        self.assertEqual(result.auto_mode_config.resume_rescan, "now")

    def test_validation_rejects_invalid_dialog_values_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "内存恢复阈值"):
                validate_settings_submission(
                    close_behavior_label="每次询问",
                    run_mode_label="全功能模式",
                    autostart_enabled=False,
                    collect_file_space=False,
                    exclude_rules_text="",
                    auto_enabled=False,
                    process_names_text="",
                    memory_pressure_enabled=False,
                    high_percent_text="80",
                    low_percent_text="79",
                    resume_rescan_label="稍后手动补扫（推荐）",
                    session_root_path=str(Path(temp_dir)),
                    close_behavior_labels=CLOSE_BEHAVIOR_LABELS,
                    run_mode_labels=RUN_MODE_LABELS,
                    auto_rescan_labels=AUTO_RESCAN_LABELS,
                )


if __name__ == "__main__":
    unittest.main()
