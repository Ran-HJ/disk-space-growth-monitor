from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from disk_monitor.benchmarking import build_default_benchmark, fixture_parameters


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the isolated v0.8.2 sequential scan benchmark baseline."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--skip-system32", action="store_true")
    parser.add_argument("--many-directories", type=int, default=24)
    parser.add_argument("--many-files-per-directory", type=int, default=400)
    parser.add_argument("--small-file-bytes", type=int, default=256)
    parser.add_argument("--large-file-count", type=int, default=8)
    parser.add_argument("--large-file-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--deep-levels", type=int, default=96)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs 必须至少为 1")

    parameters = fixture_parameters(
        many_directories=args.many_directories,
        many_files_per_directory=args.many_files_per_directory,
        small_file_bytes=args.small_file_bytes,
        large_file_count=args.large_file_count,
        large_file_bytes=args.large_file_bytes,
        deep_levels=args.deep_levels,
    )
    report = build_default_benchmark(
        runs=args.runs,
        include_system32=not args.skip_system32,
        fixture_config=parameters,
    )
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
