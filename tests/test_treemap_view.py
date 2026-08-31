from __future__ import annotations

import unittest

from disk_monitor.models import ScanItem
from disk_monitor.treemap_view import item_at, layout_rectangles


def item(name: str, size: int) -> ScanItem:
    return ScanItem(
        path=f"C:\\fixture\\{name}",
        parent_path="C:\\fixture",
        name=name,
        kind="file",
        size_bytes=size,
        file_count=1,
        depth=1,
    )


class TreemapViewTests(unittest.TestCase):
    def test_layout_fills_bounds_and_keeps_size_order_deterministic(self) -> None:
        items = [item("large.bin", 60), item("medium.bin", 30), item("small.bin", 10)]

        rectangles = layout_rectangles(items, 3, 3, 100, 50)

        self.assertEqual([rectangle[4].name for rectangle in rectangles], [
            "large.bin",
            "medium.bin",
            "small.bin",
        ])
        self.assertEqual(rectangles[0][:4], (3, 3, 60.0, 50))
        self.assertEqual(rectangles[1][:4], (63.0, 3, 40.0, 37.5))
        self.assertEqual(rectangles[2][:4], (63.0, 40.5, 40.0, 12.5))

    def test_item_at_prefers_the_last_drawn_overlapping_rectangle(self) -> None:
        first = item("first.bin", 1)
        second = item("second.bin", 1)
        rectangles = [(0.0, 0.0, 10.0, 10.0, first), (5.0, 5.0, 10.0, 10.0, second)]

        self.assertIs(item_at(rectangles, 6, 6), second)
        self.assertIs(item_at(rectangles, 2, 2), first)
        self.assertIsNone(item_at(rectangles, 20, 20))


if __name__ == "__main__":
    unittest.main()
