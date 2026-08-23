from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from disk_monitor.ui import DiskMonitorApp


class NavigationTests(unittest.TestCase):
    def test_path_chain_contains_each_filesystem_level(self) -> None:
        path = os.path.join("C:\\", "Users", "example", "Downloads")

        chain = DiskMonitorApp._path_chain(path)

        self.assertEqual(chain[0], os.path.normcase("C:\\"))
        self.assertEqual(chain[-1], os.path.normcase(path))
        self.assertEqual(len(chain), 4)

    def test_refresh_invalidates_current_path_and_descendants_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = str(Path(temp_dir) / "root")
            child = str(Path(root) / "child")
            sibling = str(Path(temp_dir) / "sibling")
            app = DiskMonitorApp.__new__(DiskMonitorApp)
            app.nav_cache = {root: object(), child: object(), sibling: object()}
            app.nav_invalidated_roots = set()

            app._invalidate_navigation_cache(root)

            self.assertNotIn(root, app.nav_cache)
            self.assertNotIn(child, app.nav_cache)
            self.assertIn(sibling, app.nav_cache)
            self.assertIn(os.path.normcase(os.path.abspath(root)), app.nav_invalidated_roots)


if __name__ == "__main__":
    unittest.main()
