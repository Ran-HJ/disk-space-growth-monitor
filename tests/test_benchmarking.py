from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from disk_monitor.benchmarking import (
    benchmark_case,
    build_fixed_fixtures,
    default_scan_options,
    run_cancellation_pressure,
    verify_permission_error_contract,
)
from disk_monitor.models import ScanProgress
from disk_monitor.scanner import ScanCancelled


class BenchmarkingTests(unittest.TestCase):
    def test_fixed_fixture_baseline_has_repeatable_structured_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixtures = build_fixed_fixtures(
                Path(temp_dir),
                many_directories=2,
                many_files_per_directory=3,
                small_file_bytes=9,
                large_file_count=2,
                large_file_bytes=17,
                deep_levels=4,
            )
            result = benchmark_case(
                "many_small",
                str(fixtures["many_small"]),
                runs=2,
                scan_options=default_scan_options(),
            )

        self.assertTrue(result["structured_results_match"])
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual(result["median"]["file_count"], 6)
        self.assertEqual(result["median"]["directory_count"], 3)

    def test_cancellation_pressure_waits_for_progress_then_returns(self) -> None:
        def cancellable_scan(
            root_path: str,
            *,
            cancel_event: threading.Event,
            progress_callback,
            **_: object,
        ):
            del root_path
            progress_callback(ScanProgress("fixture", 0, 250, 1, 0))
            cancel_event.wait(1)
            raise ScanCancelled

        result = run_cancellation_pressure(
            "fixture",
            scan_function=cancellable_scan,
            wait_for_progress_seconds=1,
        )

        self.assertTrue(result["progress_observed_before_cancel"])
        self.assertTrue(result["cancelled"])
        self.assertIsNone(result["error"])
        self.assertLess(result["response_seconds"], 1)

    def test_permission_error_contract_continues_after_denied_entry(self) -> None:
        result = verify_permission_error_contract()

        self.assertTrue(result["passed"])
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
