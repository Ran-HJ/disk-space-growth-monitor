from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from disk_monitor.models import DiskSample, SessionBoundary
from disk_monitor.trend_view import build_trend_geometry


def sample(used_bytes: int, moment: datetime) -> DiskSample:
    return DiskSample(moment, "C:\\", 1000, used_bytes, 1000 - used_bytes)


class TrendViewTests(unittest.TestCase):
    def test_geometry_preserves_gaps_boundaries_and_sample_extremes(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, 0)
        samples = [
            sample(100, now - timedelta(hours=1)),
            sample(150, now - timedelta(minutes=59)),
            sample(120, now - timedelta(minutes=45)),
        ]
        boundary = SessionBoundary(now - timedelta(minutes=30), "start")

        geometry = build_trend_geometry(
            samples,
            (boundary,),
            now=now,
            hours=1,
            width=212,
            height=112,
            gap_threshold=timedelta(minutes=5),
        )

        self.assertTrue(geometry.has_samples)
        self.assertEqual(geometry.window_start, now - timedelta(hours=1))
        self.assertEqual(geometry.window_end, now)
        self.assertEqual((geometry.low, geometry.high), (100, 150))
        self.assertEqual(len(geometry.gap_rectangles), 1)
        self.assertEqual(len(geometry.markers), 1)
        self.assertEqual(geometry.markers[0].boundary, boundary)
        self.assertEqual(len(geometry.segments), 2)
        self.assertEqual(geometry.segments[0][0], (12.0, 100.0))
        self.assertEqual(geometry.segments[1][-1][1], 64.8)

    def test_empty_samples_have_no_window_or_geometry(self) -> None:
        geometry = build_trend_geometry(
            (),
            (),
            now=datetime(2026, 8, 31, 12, 0, 0),
            hours=24,
            width=100,
            height=60,
        )

        self.assertFalse(geometry.has_samples)
        self.assertIsNone(geometry.window_start)
        self.assertEqual(geometry.segments, ())


if __name__ == "__main__":
    unittest.main()
