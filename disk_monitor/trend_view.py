from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .models import DiskSample, SessionBoundary
from .service import downsample_disk_samples, find_sample_gaps, split_sample_segments


@dataclass(frozen=True)
class TrendMarker:
    x: float
    boundary: SessionBoundary


@dataclass(frozen=True)
class TrendGeometry:
    window_start: datetime | None
    window_end: datetime | None
    low: int | None
    high: int | None
    gap_rectangles: tuple[tuple[float, float], ...] = ()
    markers: tuple[TrendMarker, ...] = ()
    segments: tuple[tuple[tuple[float, float], ...], ...] = ()

    @property
    def has_samples(self) -> bool:
        return self.low is not None and self.high is not None


def _limited_items(values: Sequence[object], maximum: int) -> list[object]:
    if len(values) <= maximum:
        return list(values)
    step = len(values) / maximum
    return [values[int(index * step)] for index in range(maximum)]


def build_trend_geometry(
    samples: Sequence[DiskSample],
    boundaries: Sequence[SessionBoundary],
    *,
    now: datetime,
    hours: int,
    width: int,
    height: int,
    padding_x: int = 12,
    padding_y: int = 12,
    max_points: int = 2_000,
    max_gaps: int = 500,
    max_segments: int = 500,
    gap_threshold: timedelta = timedelta(minutes=5),
) -> TrendGeometry:
    """Transform trend records into deterministic Canvas coordinates."""

    if hours < 1:
        raise ValueError("hours 必须至少为 1")
    if width < 2 or height < 2:
        raise ValueError("趋势图尺寸至少为 2 像素")
    if not samples:
        return TrendGeometry(None, None, None, None)
    window_end = now
    window_start = window_end - timedelta(hours=hours)
    seconds = max((window_end - window_start).total_seconds(), 1)
    available_width = width - 2 * padding_x
    available_height = height - 2 * padding_y

    def x_for(moment: datetime) -> float:
        fraction = (moment - window_start).total_seconds() / seconds
        return padding_x + max(0.0, min(fraction, 1.0)) * available_width

    values = [sample.used_bytes for sample in samples]
    low = min(values)
    high = max(values)
    value_range = max(high - low, 1)

    gaps = _limited_items(
        find_sample_gaps(samples, gap_threshold=gap_threshold), max_gaps
    )
    gap_rectangles = tuple(
        (x_for(gap_start), x_for(gap_end))
        for gap_start, gap_end in gaps
    )
    markers = tuple(
        TrendMarker(x_for(boundary.occurred_at), boundary)
        for boundary in boundaries
    )
    raw_segments = _limited_items(
        split_sample_segments(samples, gap_threshold=gap_threshold), max_segments
    )
    total_samples = max(len(samples), 1)
    segments: list[tuple[tuple[float, float], ...]] = []
    for raw_segment in raw_segments:
        segment = list(raw_segment)
        allowance = max(2, round(max_points * len(segment) / total_samples))
        drawn = downsample_disk_samples(segment, max_points=allowance)
        segments.append(
            tuple(
                (
                    x_for(sample.recorded_at),
                    height
                    - padding_y
                    - (sample.used_bytes - low) * available_height / value_range,
                )
                for sample in drawn
            )
        )
    return TrendGeometry(
        window_start,
        window_end,
        low,
        high,
        gap_rectangles=gap_rectangles,
        markers=markers,
        segments=tuple(segments),
    )
