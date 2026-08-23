from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from disk_monitor.models import DiskSample, MonitorSession
from disk_monitor.service import (
    BLIND_SPOT_THRESHOLD_BYTES,
    calculate_blind_spot,
    continuous_baseline_snapshot_id,
    downsample_disk_samples,
    find_sample_gaps,
    nearest_disk_sample,
    split_sample_segments,
)


def sample(used_bytes: int, recorded_at: datetime) -> DiskSample:
    return DiskSample(recorded_at, "C:\\", 10**12, used_bytes, 10**12 - used_bytes)


class ServiceTests(unittest.TestCase):
    def test_blind_spot_requires_old_sample_and_strictly_exceeds_threshold(self) -> None:
        now = datetime.now()
        current = sample(500_000_000, now)

        missing = calculate_blind_spot(current, None)
        exact = calculate_blind_spot(
            current,
            sample(500_000_000 - BLIND_SPOT_THRESHOLD_BYTES, now - timedelta(hours=2)),
        )
        growth = calculate_blind_spot(
            current,
            sample(
                500_000_000 - BLIND_SPOT_THRESHOLD_BYTES - 1,
                now - timedelta(days=2),
            ),
        )
        decrease = calculate_blind_spot(
            current,
            sample(
                500_000_000 + BLIND_SPOT_THRESHOLD_BYTES + 1,
                now - timedelta(hours=3),
            ),
        )

        self.assertIsNone(missing.change_bytes)
        self.assertFalse(missing.is_significant)
        self.assertFalse(exact.is_significant)
        self.assertTrue(growth.is_significant)
        self.assertGreater(growth.change_bytes or 0, 0)
        self.assertTrue(decrease.is_significant)
        self.assertLess(decrease.change_bytes or 0, 0)

    def test_continuous_baseline_requires_full_close_and_same_path(self) -> None:
        now = datetime.now()
        session = MonitorSession(
            id=1,
            drive="C:\\",
            root_path="C:\\data",
            started_at=now,
            ended_at=now,
            start_used_bytes=100,
            end_used_bytes=120,
            start_snapshot_id=10,
            end_snapshot_id=11,
            end_reason="normal_close",
            status="completed",
        )

        self.assertEqual(
            continuous_baseline_snapshot_id(session, "C:\\data"), 11
        )
        self.assertIsNone(
            continuous_baseline_snapshot_id(session, "C:\\other")
        )
        quick_session = MonitorSession(
            **{**session.__dict__, "end_reason": "quick_close"}
        )
        self.assertIsNone(
            continuous_baseline_snapshot_id(quick_session, "C:\\data")
        )

    def test_trend_downsampling_preserves_endpoints(self) -> None:
        start = datetime.now() - timedelta(minutes=99)
        samples = [
            sample(index, start + timedelta(minutes=index))
            for index in range(100)
        ]

        reduced = downsample_disk_samples(samples, max_points=12)

        self.assertLessEqual(len(reduced), 12)
        self.assertEqual(reduced[0], samples[0])
        self.assertEqual(reduced[-1], samples[-1])
        self.assertEqual(
            [item.recorded_at for item in reduced],
            sorted(item.recorded_at for item in reduced),
        )

    def test_trend_gap_detection_and_nearest_sample(self) -> None:
        start = datetime.now()
        samples = [
            sample(100, start),
            sample(110, start + timedelta(minutes=1)),
            sample(130, start + timedelta(minutes=10)),
        ]

        gaps = find_sample_gaps(samples)
        segments = split_sample_segments(samples)
        nearest = nearest_disk_sample(
            samples, start + timedelta(minutes=9)
        )

        self.assertEqual(
            gaps,
            [(start + timedelta(minutes=1), start + timedelta(minutes=10))],
        )
        self.assertEqual([len(segment) for segment in segments], [2, 1])
        self.assertEqual(nearest, samples[-1])


if __name__ == "__main__":
    unittest.main()
