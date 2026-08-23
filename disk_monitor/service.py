from __future__ import annotations

import os
import shutil
from bisect import bisect_left
from datetime import datetime
from datetime import timedelta
from collections.abc import Sequence

from .models import BlindSpotResult, DiskSample, MonitorSession


BLIND_SPOT_THRESHOLD_BYTES = 100 * 1024 * 1024


def normalize_drive(path: str) -> str:
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    return (drive + "\\") if drive else os.path.abspath(path)


def read_disk_sample(path: str = "C:\\") -> DiskSample:
    drive = normalize_drive(path)
    total, used, free = shutil.disk_usage(drive)
    return DiskSample(
        recorded_at=datetime.now(),
        drive=drive,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
    )


def calculate_blind_spot(
    current: DiskSample,
    previous: DiskSample | None,
    *,
    threshold_bytes: int = BLIND_SPOT_THRESHOLD_BYTES,
) -> BlindSpotResult:
    if threshold_bytes < 0:
        raise ValueError("threshold_bytes 不能为负数")
    return BlindSpotResult(
        current_at=current.recorded_at,
        previous_at=previous.recorded_at if previous else None,
        change_bytes=(
            current.used_bytes - previous.used_bytes if previous else None
        ),
        threshold_bytes=threshold_bytes,
    )


def continuous_baseline_snapshot_id(
    session: MonitorSession | None, current_root_path: str
) -> int | None:
    if session is None:
        return None
    same_path = os.path.normcase(os.path.abspath(session.root_path)) == os.path.normcase(
        os.path.abspath(current_root_path)
    )
    if (
        session.end_reason == "normal_close"
        and session.end_snapshot_id is not None
        and same_path
    ):
        return session.end_snapshot_id
    return None


def downsample_disk_samples(
    samples: Sequence[DiskSample], max_points: int = 2_000
) -> list[DiskSample]:
    if max_points < 2:
        raise ValueError("max_points 必须至少为 2")
    if len(samples) <= max_points:
        return list(samples)
    last_index = len(samples) - 1
    indexes = {
        round(index * last_index / (max_points - 1))
        for index in range(max_points)
    }
    return [samples[index] for index in sorted(indexes)]


def split_sample_segments(
    samples: Sequence[DiskSample],
    *,
    gap_threshold: timedelta = timedelta(minutes=5),
) -> list[list[DiskSample]]:
    if gap_threshold <= timedelta(0):
        raise ValueError("gap_threshold 必须大于 0")
    if not samples:
        return []
    segments: list[list[DiskSample]] = [[samples[0]]]
    for previous, current in zip(samples, samples[1:]):
        if current.recorded_at - previous.recorded_at > gap_threshold:
            segments.append([])
        segments[-1].append(current)
    return segments


def find_sample_gaps(
    samples: Sequence[DiskSample],
    *,
    gap_threshold: timedelta = timedelta(minutes=5),
) -> list[tuple[datetime, datetime]]:
    return [
        (previous.recorded_at, current.recorded_at)
        for previous, current in zip(samples, samples[1:])
        if current.recorded_at - previous.recorded_at > gap_threshold
    ]


def nearest_disk_sample(
    samples: Sequence[DiskSample], target_time: datetime
) -> DiskSample | None:
    if not samples:
        return None
    timestamps = [sample.recorded_at for sample in samples]
    index = bisect_left(timestamps, target_time)
    if index <= 0:
        return samples[0]
    if index >= len(samples):
        return samples[-1]
    before = samples[index - 1]
    after = samples[index]
    if target_time - before.recorded_at <= after.recorded_at - target_time:
        return before
    return after
