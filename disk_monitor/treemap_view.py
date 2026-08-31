from __future__ import annotations

from collections.abc import Sequence

from .models import ScanItem


TreemapRectangle = tuple[float, float, float, float, ScanItem]


def layout_rectangles(
    items: Sequence[ScanItem],
    x: float,
    y: float,
    width: float,
    height: float,
) -> list[TreemapRectangle]:
    """Lay out size-sorted direct items with the existing deterministic split rule."""

    rectangles: list[TreemapRectangle] = []

    def layout(
        current_items: Sequence[ScanItem],
        current_x: float,
        current_y: float,
        current_width: float,
        current_height: float,
    ) -> None:
        if not current_items or current_width <= 0 or current_height <= 0:
            return
        if len(current_items) == 1:
            rectangles.append(
                (
                    current_x,
                    current_y,
                    current_width,
                    current_height,
                    current_items[0],
                )
            )
            return
        total = sum(item.size_bytes for item in current_items)
        split_target = total / 2
        accumulated = 0
        split_index = 1
        for index, item in enumerate(current_items[:-1], start=1):
            accumulated += item.size_bytes
            split_index = index
            if accumulated >= split_target:
                break
        first = current_items[:split_index]
        second = current_items[split_index:]
        first_total = sum(item.size_bytes for item in first)
        ratio = first_total / total if total else 0.5
        if current_width >= current_height:
            first_width = current_width * ratio
            layout(first, current_x, current_y, first_width, current_height)
            layout(
                second,
                current_x + first_width,
                current_y,
                current_width - first_width,
                current_height,
            )
        else:
            first_height = current_height * ratio
            layout(first, current_x, current_y, current_width, first_height)
            layout(
                second,
                current_x,
                current_y + first_height,
                current_width,
                current_height - first_height,
            )

    layout(items, x, y, width, height)
    return rectangles


def item_at(
    rectangles: Sequence[TreemapRectangle], x: float, y: float
) -> ScanItem | None:
    """Return the topmost rectangle under a point using the current draw order."""

    for left, top, width, height, item in reversed(rectangles):
        if left <= x <= left + width and top <= y <= top + height:
            return item
    return None
