from __future__ import annotations

import unittest

from disk_monitor.accounting import compare_recorded_accounting


def snapshot(snapshot_id: int, unique_size: int, *, state: str = "exact"):
    return {
        "id": snapshot_id,
        "root_path": "C:\\root",
        "total_bytes": unique_size,
        "allocated_total_bytes": unique_size,
        "unique_allocated_total_bytes": (
            unique_size if state == "exact" else None
        ),
        "measurement_state": state,
    }


def item(
    path: str,
    allocated_size: int,
    *,
    owner: bool,
    file_id: bytes = b"\x01" * 16,
):
    return {
        "path": path,
        "kind": "file",
        "allocated_size_bytes": allocated_size,
        "volume_serial_hex": "000000001234abcd",
        "file_id": file_id,
        "link_count": 2,
        "is_unique_owner": owner,
        "measurement_state": "exact",
    }


class AccountingComparisonTests(unittest.TestCase):
    def test_alias_change_is_not_unique_growth(self) -> None:
        old_rows = [item("C:\\root\\z.bin", 4096, owner=True)]
        new_rows = [
            item("C:\\root\\a.bin", 4096, owner=True),
            item("C:\\root\\z.bin", 4096, owner=False),
        ]

        result = compare_recorded_accounting(
            snapshot(2, 4096), snapshot(1, 4096), new_rows, old_rows
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["unique_allocated_total_change_bytes"], 0)
        self.assertEqual(result["verified_item_change_bytes"], 0)
        self.assertEqual(len(result["items"]), 1)
        change = result["items"][0]
        self.assertEqual(change["change_bytes"], 0)
        self.assertEqual(change["change_kind"], "accounting_path_changed")
        self.assertTrue(change["paths_changed"])
        self.assertTrue(change["ownership_changed"])

    def test_content_growth_is_counted_once_for_all_aliases(self) -> None:
        old_rows = [
            item("C:\\root\\a.bin", 4096, owner=True),
            item("C:\\root\\z.bin", 4096, owner=False),
        ]
        new_rows = [
            item("C:\\root\\a.bin", 8192, owner=True),
            item("C:\\root\\z.bin", 8192, owner=False),
        ]

        result = compare_recorded_accounting(
            snapshot(2, 8192), snapshot(1, 4096), new_rows, old_rows
        )

        self.assertEqual(result["unique_allocated_total_change_bytes"], 4096)
        self.assertEqual(result["verified_item_change_bytes"], 4096)
        self.assertEqual(result["unattributed_unique_change_bytes"], 0)
        self.assertEqual(result["items"][0]["change_bytes"], 4096)
        self.assertEqual(result["items"][0]["change_kind"], "content_size_changed")

    def test_one_sided_record_is_unresolved_instead_of_claimed(self) -> None:
        result = compare_recorded_accounting(
            snapshot(2, 8192),
            snapshot(1, 4096),
            [item("C:\\root\\new.bin", 8192, owner=True)],
            [],
        )

        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["unresolved_items"]), 1)
        self.assertEqual(result["verified_item_change_bytes"], 0)
        self.assertEqual(result["unattributed_unique_change_bytes"], 4096)

    def test_partial_snapshot_disables_exact_comparison(self) -> None:
        result = compare_recorded_accounting(
            snapshot(2, 4096, state="partial"),
            snapshot(1, 4096),
            [],
            [],
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "measurement_coverage_incomplete")


if __name__ == "__main__":
    unittest.main()
