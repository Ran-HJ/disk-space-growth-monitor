from __future__ import annotations

import ctypes
import os
import platform
import statistics
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .models import NavigationItem, NavigationNode, ScanItem, ScanProgress, ScanResult
from .scanner import ScanCancelled, scan_path


ScanFunction = Callable[..., ScanResult]


@dataclass(frozen=True)
class ScanMeasurement:
    elapsed_seconds: float
    peak_working_set_bytes: int | None
    total_bytes: int
    file_count: int
    directory_count: int
    error_count: int
    excluded_item_count: int

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "peak_working_set_bytes": self.peak_working_set_bytes,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "error_count": self.error_count,
            "excluded_item_count": self.excluded_item_count,
        }


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def working_set_bytes() -> int | None:
    """Return the current process working set on Windows, otherwise None."""

    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return int(counters.WorkingSetSize)


def default_scan_options() -> dict[str, int | bool]:
    """Use the normal logical-size scan path for comparable default baselines."""

    return {
        "record_depth": 2,
        "top_file_limit": 200,
        "navigation_file_limit": 20,
        "navigation_total_file_limit": 20_000,
        "collect_file_space": False,
    }


def build_fixed_fixtures(
    root: Path,
    *,
    many_directories: int = 24,
    many_files_per_directory: int = 400,
    small_file_bytes: int = 256,
    large_file_count: int = 8,
    large_file_bytes: int = 4 * 1024 * 1024,
    deep_levels: int = 96,
) -> dict[str, Path]:
    """Build deterministic benchmark fixtures below a caller-owned temporary root."""

    values = {
        "many_directories": many_directories,
        "many_files_per_directory": many_files_per_directory,
        "small_file_bytes": small_file_bytes,
        "large_file_count": large_file_count,
        "large_file_bytes": large_file_bytes,
        "deep_levels": deep_levels,
    }
    if any(value < 1 for value in values.values()):
        raise ValueError("基准夹具参数必须均为正数")

    root.mkdir(parents=True, exist_ok=True)
    many_small = root / "many-small"
    small_payload = b"s" * small_file_bytes
    for directory_index in range(many_directories):
        directory = many_small / f"d{directory_index:03d}"
        directory.mkdir(parents=True)
        for file_index in range(many_files_per_directory):
            (directory / f"f{file_index:04d}.bin").write_bytes(small_payload)

    few_large = root / "few-large"
    few_large.mkdir()
    large_payload = b"L" * large_file_bytes
    for file_index in range(large_file_count):
        (few_large / f"large-{file_index:03d}.bin").write_bytes(large_payload)

    deep = root / "deep"
    current = deep
    for _ in range(deep_levels):
        current = current / "d"
        current.mkdir(parents=True)
    (current / "leaf.bin").write_bytes(b"d" * small_file_bytes)

    return {
        "many_small": many_small,
        "few_large": few_large,
        "deep": deep,
    }


def fixture_parameters(
    *,
    many_directories: int,
    many_files_per_directory: int,
    small_file_bytes: int,
    large_file_count: int,
    large_file_bytes: int,
    deep_levels: int,
) -> dict[str, int]:
    return {
        "many_directories": many_directories,
        "many_files_per_directory": many_files_per_directory,
        "small_file_bytes": small_file_bytes,
        "large_file_count": large_file_count,
        "large_file_bytes": large_file_bytes,
        "deep_levels": deep_levels,
    }


def _item_signature(item: ScanItem) -> tuple[object, ...]:
    return (
        item.path,
        item.parent_path,
        item.name,
        item.kind,
        item.size_bytes,
        item.file_count,
        item.depth,
        item.modified_at,
        item.allocated_size_bytes,
        item.unique_allocated_size_bytes,
        item.volume_serial_hex,
        item.file_id,
        item.link_count,
        item.is_unique_owner,
        item.measurement_state,
    )


def _navigation_item_signature(item: NavigationItem) -> tuple[object, ...]:
    return (
        item.name,
        item.kind,
        item.size_bytes,
        item.file_count,
        item.modified_at,
        item.allocated_size_bytes,
        item.unique_allocated_size_bytes,
        item.volume_serial_hex,
        item.file_id,
        item.link_count,
        item.is_unique_owner,
        item.measurement_state,
    )


def _navigation_node_signature(node: NavigationNode) -> tuple[object, ...]:
    return (
        node.total_bytes,
        node.file_count,
        node.directory_count,
        node.error_count,
        node.modified_at,
        node.direct_file_bytes,
        node.direct_file_count,
        tuple(sorted(_navigation_item_signature(item) for item in node.children)),
        node.allocated_size_bytes,
        node.unique_allocated_size_bytes,
        node.measured_allocated_bytes,
        node.measured_unique_allocated_bytes,
        node.eligible_file_count,
        node.allocation_measured_file_count,
        node.identity_measured_file_count,
        node.metadata_error_count,
        node.measurement_state,
    )


def scan_structure_signature(result: ScanResult) -> tuple[object, ...]:
    """Compare scan products directly; intentionally do not derive a hash."""

    skeleton = result.skeleton
    skeleton_signature = None
    if skeleton is not None:
        skeleton_signature = (
            skeleton.root_path,
            skeleton.estimated_bytes,
            skeleton.degraded,
            tuple(
                sorted(
                    (path, _navigation_node_signature(node))
                    for path, node in skeleton.nodes.items()
                )
            ),
        )
    return (
        result.root_path,
        result.total_bytes,
        result.file_count,
        result.directory_count,
        result.error_count,
        tuple(_item_signature(item) for item in result.items),
        skeleton_signature,
        result.allocated_total_bytes,
        result.unique_allocated_total_bytes,
        result.measured_allocated_bytes,
        result.measured_unique_allocated_bytes,
        result.eligible_file_count,
        result.allocation_measured_file_count,
        result.identity_measured_file_count,
        result.metadata_error_count,
        result.measurement_state,
        result.scan_config_version,
        result.scan_config_json,
        result.excluded_rule_count,
        result.excluded_item_count,
    )


def measure_scan(
    root_path: str,
    *,
    scan_options: Mapping[str, int | bool] | None = None,
    scan_function: ScanFunction = scan_path,
) -> tuple[ScanMeasurement, tuple[object, ...]]:
    """Measure one completed scan and retain its direct comparison structure."""

    options = dict(default_scan_options() if scan_options is None else scan_options)
    samples: list[int] = []
    baseline = working_set_bytes()
    if baseline is not None:
        samples.append(baseline)
    sampling_done = threading.Event()

    def sample_memory() -> None:
        while not sampling_done.wait(0.01):
            current = working_set_bytes()
            if current is not None:
                samples.append(current)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        result = scan_function(root_path, **options)
    finally:
        sampling_done.set()
        sampler.join(timeout=2)
    finished = time.perf_counter()
    current = working_set_bytes()
    if current is not None:
        samples.append(current)
    measurement = ScanMeasurement(
        elapsed_seconds=finished - started,
        peak_working_set_bytes=max(samples) if samples else None,
        total_bytes=result.total_bytes,
        file_count=result.file_count,
        directory_count=result.directory_count,
        error_count=result.error_count,
        excluded_item_count=result.excluded_item_count,
    )
    return measurement, scan_structure_signature(result)


def benchmark_case(
    label: str,
    root_path: str,
    *,
    runs: int = 3,
    mode: str = "sequential",
    workers: int = 1,
    scan_options: Mapping[str, int | bool] | None = None,
    scan_function: ScanFunction = scan_path,
) -> dict[str, object]:
    """Run one fixture repeatedly and compare complete structured results."""

    if runs < 1:
        raise ValueError("runs 必须至少为 1")
    if workers < 1:
        raise ValueError("workers 必须至少为 1")
    measurements: list[ScanMeasurement] = []
    signatures: list[tuple[object, ...]] = []
    for _ in range(runs):
        measurement, signature = measure_scan(
            root_path,
            scan_options=scan_options,
            scan_function=scan_function,
        )
        measurements.append(measurement)
        signatures.append(signature)

    first = measurements[0]
    peak_values = [
        measurement.peak_working_set_bytes
        for measurement in measurements
        if measurement.peak_working_set_bytes is not None
    ]
    return {
        "fixture": label,
        "mode": mode,
        "workers": workers,
        "runs": [measurement.as_dict() for measurement in measurements],
        "median": {
            "elapsed_seconds": round(
                statistics.median(
                    measurement.elapsed_seconds for measurement in measurements
                ),
                6,
            ),
            "peak_working_set_bytes": (
                int(statistics.median(peak_values)) if peak_values else None
            ),
            "total_bytes": first.total_bytes,
            "file_count": first.file_count,
            "directory_count": first.directory_count,
            "error_count": first.error_count,
            "excluded_item_count": first.excluded_item_count,
        },
        "structured_results_match": all(
            signature == signatures[0] for signature in signatures[1:]
        ),
    }


def run_cancellation_pressure(
    root_path: str,
    *,
    scan_options: Mapping[str, int | bool] | None = None,
    wait_for_progress_seconds: float = 10.0,
    scan_function: ScanFunction = scan_path,
) -> dict[str, object]:
    """Request cancellation after observable progress and measure safe return time."""

    if wait_for_progress_seconds <= 0:
        raise ValueError("wait_for_progress_seconds 必须为正数")
    options = dict(default_scan_options() if scan_options is None else scan_options)
    cancel_event = threading.Event()
    progress_seen = threading.Event()
    completed = threading.Event()
    outcome: dict[str, object] = {"cancelled": False, "error": None}

    def report(_: ScanProgress) -> None:
        progress_seen.set()

    def worker() -> None:
        try:
            scan_function(
                root_path,
                cancel_event=cancel_event,
                progress_callback=report,
                **options,
            )
        except ScanCancelled:
            outcome["cancelled"] = True
        except BaseException as error:  # capture for an explicit benchmark record
            outcome["error"] = type(error).__name__
        finally:
            completed.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    progress_observed = progress_seen.wait(wait_for_progress_seconds)
    requested_at = time.perf_counter()
    cancel_event.set()
    thread.join()
    return {
        "progress_observed_before_cancel": progress_observed,
        "completed_before_cancel": completed.is_set() and not progress_observed,
        "cancelled": bool(outcome["cancelled"]),
        "error": outcome["error"],
        "response_seconds": round(time.perf_counter() - requested_at, 6),
    }


def verify_permission_error_contract() -> dict[str, int | bool]:
    """Exercise error counting/continuation without changing any real ACL."""

    root = os.path.normcase(os.path.abspath("benchmark-permission-root"))

    class Entry:
        def __init__(self, name: str, *, denied: bool) -> None:
            self.name = name
            self.path = os.path.join(root, name)
            self.denied = denied

        def stat(self, *, follow_symlinks: bool = False):
            del follow_symlinks
            if self.denied:
                raise PermissionError("fixture permission denied")
            return SimpleNamespace(
                st_file_attributes=0,
                st_size=7,
                st_mtime=1.0,
            )

        def is_symlink(self) -> bool:
            return False

        def is_dir(self, *, follow_symlinks: bool = False) -> bool:
            del follow_symlinks
            return False

        def is_file(self, *, follow_symlinks: bool = False) -> bool:
            del follow_symlinks
            return True

    class Scandir:
        def __enter__(self):
            return iter((Entry("denied.bin", denied=True), Entry("kept.bin", denied=False)))

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback

    with patch("disk_monitor.scanner.os.path.isdir", return_value=True), patch(
        "disk_monitor.scanner.os.scandir", return_value=Scandir()
    ):
        result = scan_path(root)
    passed = (
        result.total_bytes == 7
        and result.file_count == 1
        and result.directory_count == 1
        and result.error_count == 1
    )
    return {
        "passed": passed,
        "total_bytes": result.total_bytes,
        "file_count": result.file_count,
        "directory_count": result.directory_count,
        "error_count": result.error_count,
    }


def environment_summary() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "implementation": platform.python_implementation(),
    }


def build_default_benchmark(
    *,
    runs: int,
    include_system32: bool,
    fixture_config: Mapping[str, int],
) -> dict[str, object]:
    """Build, run, and clean the baseline fixtures in one isolated operation."""

    with tempfile.TemporaryDirectory(prefix="DiskMonitorScanBenchmark-") as temp_dir:
        fixtures = build_fixed_fixtures(Path(temp_dir), **fixture_config)
        cases = [
            benchmark_case(label, str(path), runs=runs)
            for label, path in fixtures.items()
        ]
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        if include_system32 and system32.is_dir():
            cases.append(benchmark_case("system32_readonly", str(system32), runs=runs))
        cancellation = run_cancellation_pressure(str(fixtures["many_small"]))
        permission_contract = verify_permission_error_contract()
    return {
        "environment": environment_summary(),
        "runs_per_case": runs,
        "mode": "sequential",
        "workers": 1,
        "fixture_parameters": dict(fixture_config),
        "system32_included": include_system32 and system32.is_dir(),
        "cases": cases,
        "cancellation_pressure": cancellation,
        "permission_error_contract": permission_contract,
        "comparison_policy": "direct structured fields; no content, result, or database hashes",
    }
