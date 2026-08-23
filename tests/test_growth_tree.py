from __future__ import annotations

import unittest

from disk_monitor.growth_tree import GrowthTreeNode, build_growth_tree
from disk_monitor.models import GrowthItem


def item(
    path: str,
    parent_path: str,
    name: str,
    change: int,
    *,
    kind: str = "directory",
) -> GrowthItem:
    return GrowthItem(
        path=path,
        parent_path=parent_path,
        name=name,
        kind=kind,
        old_size_bytes=100,
        new_size_bytes=100 + change,
    )


def numeric_leaves(nodes: list[GrowthTreeNode]) -> list[int]:
    values: list[int] = []
    for node in nodes:
        if node.change_bytes is not None:
            values.append(node.change_bytes)
        values.extend(numeric_leaves(node.children))
    return values


class GrowthTreeTests(unittest.TestCase):
    def test_same_growth_chain_has_one_numeric_leaf(self) -> None:
        changes = [
            item("c:\\", "c:\\", "c:\\", 15),
            item("c:\\users", "c:\\", "Users", 10),
            item(
                "c:\\users\\data.bin",
                "c:\\users",
                "data.bin",
                10,
                kind="file",
            ),
            item("c:\\windows", "c:\\", "Windows", 5),
        ]

        tree = build_growth_tree(changes)

        self.assertEqual(len(tree), 1)
        self.assertIsNone(tree[0].change_bytes)
        self.assertCountEqual(numeric_leaves(tree), [10, 5])
        self.assertEqual(sum(numeric_leaves(tree)), 15)

    def test_unattributed_difference_is_kept_as_residual_leaf(self) -> None:
        changes = [
            item("c:\\", "c:\\", "c:\\", 12),
            item("c:\\users", "c:\\", "Users", 10),
        ]

        tree = build_growth_tree(changes)

        self.assertCountEqual(numeric_leaves(tree), [10, 2])
        residual = next(
            child for child in tree[0].children if child.item is None
        )
        self.assertEqual(residual.label, "其他未展开变化")

    def test_decrease_tree_keeps_negative_values(self) -> None:
        changes = [
            item("c:\\", "c:\\", "c:\\", -12),
            item("c:\\users", "c:\\", "Users", -10),
        ]

        tree = build_growth_tree(changes)

        self.assertCountEqual(numeric_leaves(tree), [-10, -2])


if __name__ == "__main__":
    unittest.main()
