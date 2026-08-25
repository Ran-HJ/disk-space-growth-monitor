from __future__ import annotations

import os
import unittest

from disk_monitor.automation import (
    AutoModeConfig,
    AutoModePolicy,
    AutoObservation,
    WindowsSystemProbe,
    normalize_process_names,
)


def observation(memory: int, *processes: str) -> AutoObservation:
    return AutoObservation(memory, tuple(processes), "2026-08-25T20:00:00+08:00")


class MemorySettings:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get_setting(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value


class AutomationTests(unittest.TestCase):
    def test_process_names_are_normalized_and_deduplicated(self) -> None:
        self.assertEqual(
            normalize_process_names('CS2; "R5Apex.exe"\ncs2.exe;D:\\Game\\Foo'),
            ("cs2.exe", "r5apex.exe", "foo.exe"),
        )

    def test_config_round_trip_and_invalid_stored_values_fall_back(self) -> None:
        settings = MemorySettings()
        config = AutoModeConfig(
            enabled=True,
            process_names=("game.exe",),
            memory_pressure_enabled=False,
            high_percent=90,
            low_percent=70,
            resume_rescan="now",
        )
        config.save(settings)
        self.assertEqual(AutoModeConfig.load(settings), config)

        settings.values["auto_memory_high_percent"] = "20"
        self.assertEqual(AutoModeConfig.load(settings), AutoModeConfig())

    def test_process_trigger_waits_for_stability_and_active_scan(self) -> None:
        policy = AutoModePolicy(
            AutoModeConfig(enabled=True, memory_pressure_enabled=False)
        )
        first = policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=False
        )
        self.assertIsNone(first.action)
        self.assertEqual(first.status, "detecting")

        waiting = policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=True
        )
        self.assertIsNone(waiting.action)
        self.assertEqual(waiting.status, "waiting_for_scan")

        ready = policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=False
        )
        self.assertEqual(ready.action, "enter_low")

    def test_memory_hysteresis_and_stable_recovery(self) -> None:
        config = AutoModeConfig(
            enabled=True,
            process_names=(),
            enter_samples=2,
            exit_samples=3,
        )
        policy = AutoModePolicy(config)
        policy.evaluate(observation(86), run_mode="full", scan_busy=False)
        enter = policy.evaluate(
            observation(87), run_mode="full", scan_busy=False
        )
        self.assertEqual(enter.action, "enter_low")
        policy.mark_auto_entered()

        latched = policy.evaluate(
            observation(80), run_mode="low_memory", scan_busy=False
        )
        self.assertEqual(latched.status, "auto_low")
        self.assertIn("memory_pressure", latched.triggers)

        for _ in range(2):
            recovery = policy.evaluate(
                observation(74), run_mode="low_memory", scan_busy=False
            )
            self.assertIsNone(recovery.action)
        leave = policy.evaluate(
            observation(74), run_mode="low_memory", scan_busy=False
        )
        self.assertEqual(leave.action, "leave_low")

    def test_manual_low_mode_is_never_automatically_resumed(self) -> None:
        policy = AutoModePolicy(AutoModeConfig(enabled=True, exit_samples=1))
        decision = policy.evaluate(
            observation(30), run_mode="low_memory", scan_busy=False
        )
        self.assertIsNone(decision.action)
        self.assertEqual(decision.status, "manual_low")

    def test_manual_override_waits_until_trigger_clears_before_rearming(self) -> None:
        policy = AutoModePolicy(
            AutoModeConfig(enabled=True, memory_pressure_enabled=False)
        )
        policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=False
        )
        policy.note_manual_mode_change()

        held = policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=False
        )
        self.assertEqual(held.status, "manual_override")
        cleared = policy.evaluate(
            observation(40), run_mode="full", scan_busy=False
        )
        self.assertEqual(cleared.status, "monitoring")
        rearmed = policy.evaluate(
            observation(40, "cs2.exe"), run_mode="full", scan_busy=False
        )
        self.assertEqual(rearmed.status, "detecting")

    @unittest.skipUnless(os.name == "nt", "Windows 系统探针仅在 Windows 运行")
    def test_windows_probe_reads_memory_and_processes(self) -> None:
        result = WindowsSystemProbe().observe(("python.exe",))
        self.assertGreaterEqual(result.memory_percent, 0)
        self.assertLessEqual(result.memory_percent, 100)
        self.assertIn("python.exe", result.matched_processes)
        self.assertTrue(result.observed_at)


if __name__ == "__main__":
    unittest.main()
