import unittest

from disk_monitor.formatting import format_bytes


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(-1536), "-1.5 KB")


if __name__ == "__main__":
    unittest.main()

