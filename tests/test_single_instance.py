from __future__ import annotations

import os
import unittest
import uuid

from disk_monitor.single_instance import SingleInstance


@unittest.skipUnless(os.name == "nt", "单实例互斥锁只适用于 Windows")
class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected_until_first_releases(self) -> None:
        name = f"Local\\DiskGrowthMonitorTest-{uuid.uuid4()}"
        first = SingleInstance(name)
        second = SingleInstance(name)
        third = SingleInstance(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()


if __name__ == "__main__":
    unittest.main()

