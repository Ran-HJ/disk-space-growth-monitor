from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import GrowthItem


@dataclass
class GrowthTreeNode:
    label: str
    current_bytes: int | None
    change_bytes: int | None
    item: GrowthItem | None = None
    children: list["GrowthTreeNode"] = field(default_factory=list)


def build_growth_tree(items: list[GrowthItem]) -> list[GrowthTreeNode]:
    """把平铺的父子变化折叠为只在叶子显示数值的树。"""

    if not items:
        return []
    nodes = {
        item.path: GrowthTreeNode(
            label=item.name,
            current_bytes=item.new_size_bytes,
            change_bytes=item.change_bytes,
            item=item,
        )
        for item in items
    }
    roots: list[GrowthTreeNode] = []
    for item in items:
        parent_path = _nearest_changed_parent(item, nodes)
        if parent_path is None:
            roots.append(nodes[item.path])
        else:
            nodes[parent_path].children.append(nodes[item.path])

    def finish(node: GrowthTreeNode) -> None:
        for child in node.children:
            finish(child)
        node.children.sort(
            key=lambda child: abs(child.item.change_bytes) if child.item else 0,
            reverse=True,
        )
        if not node.children or node.item is None:
            return
        represented = sum(
            child.item.change_bytes
            for child in node.children
            if child.item is not None
        )
        residual = node.item.change_bytes - represented
        if residual:
            node.children.append(
                GrowthTreeNode(
                    label="其他未展开变化",
                    current_bytes=None,
                    change_bytes=residual,
                )
            )
        node.change_bytes = None

    for root in roots:
        finish(root)
    roots.sort(
        key=lambda node: abs(node.item.change_bytes) if node.item else 0,
        reverse=True,
    )
    return roots


def _nearest_changed_parent(
    item: GrowthItem, nodes: dict[str, GrowthTreeNode]
) -> str | None:
    parent = item.parent_path or os.path.dirname(item.path)
    seen: set[str] = set()
    while parent and parent not in seen and parent != item.path:
        if parent in nodes:
            return parent
        seen.add(parent)
        next_parent = os.path.dirname(parent.rstrip("\\/"))
        if next_parent == parent:
            break
        parent = next_parent
    return None
