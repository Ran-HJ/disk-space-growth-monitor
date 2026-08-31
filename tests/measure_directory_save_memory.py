from __future__ import annotations

import argparse
import ctypes
import gc
import json
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from disk_monitor.models import DirectorySkeleton, NavigationNode, ScanResult
from disk_monitor.storage import Storage


class ProcessMemoryCounters(ctypes.Structure):
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


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
_KERNEL32.GetCurrentProcess.restype = ctypes.c_void_p
_PSAPI.GetProcessMemoryInfo.argtypes = (
    ctypes.c_void_p,
    ctypes.POINTER(ProcessMemoryCounters),
    ctypes.c_ulong,
)
_PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int


def working_set_bytes() -> int:
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = _KERNEL32.GetCurrentProcess()
    if not _PSAPI.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return int(counters.WorkingSetSize)


def synthetic_path(index: int, target_length: int) -> str:
    prefix = f"c:\\synthetic\\{index:08d}\\"
    return prefix + ("x" * max(target_length - len(prefix), 1))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure one schema-v3 full-directory save working-set peak."
    )
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--path-length", type=int, required=True)
    args = parser.parse_args()
    if args.count < 1 or args.path_length < 10:
        parser.error("count 和 path-length 必须为正数")

    now = datetime.now()
    nodes = {
        synthetic_path(index, args.path_length): NavigationNode(
            total_bytes=index * 4096,
            file_count=index % 1000,
            directory_count=index % 100,
            error_count=0,
            modified_at=1_700_000_000.0 + index,
            direct_file_bytes=(index % 10) * 4096,
            direct_file_count=index % 10,
        )
        for index in range(1, args.count + 1)
    }
    root_path = next(iter(nodes))
    result = ScanResult(
        root_path=root_path,
        started_at=now,
        finished_at=now,
        total_bytes=args.count * 4096,
        file_count=args.count,
        directory_count=args.count,
        error_count=0,
        skeleton=DirectorySkeleton(root_path, now, now, nodes),
        scan_config_version=1,
        scan_config_json="{}",
    )
    with tempfile.TemporaryDirectory(prefix="DiskMonitorDirectorySave-") as temp:
        storage = Storage(Path(temp) / "monitor.db")
        gc.collect()
        baseline = working_set_bytes()
        samples = [baseline]
        finished = threading.Event()

        def sample_memory() -> None:
            while not finished.wait(0.01):
                samples.append(working_set_bytes())

        sampler = threading.Thread(target=sample_memory, daemon=True)
        sampler.start()
        started = time.perf_counter()
        try:
            storage.save_scan(result, source="baseline")
        finally:
            finished.set()
            sampler.join(timeout=2)
        samples.append(working_set_bytes())
        elapsed = time.perf_counter() - started
        database_bytes = storage.database_path.stat().st_size
    peak = max(samples)
    increase = max(peak - baseline, 0)
    print(
        json.dumps(
            {
                "directory_count": args.count,
                "path_characters": args.path_length,
                "baseline_working_set_bytes": baseline,
                "peak_working_set_bytes": peak,
                "increase_bytes": increase,
                "increase_percent": round(increase / baseline * 100, 2),
                "save_elapsed_seconds": round(elapsed, 3),
                "database_bytes": database_bytes,
                "measurement": "working set sampled every 10 ms during one save",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
