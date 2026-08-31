from __future__ import annotations

import logging
import tempfile
import unittest

from disk_monitor.control_bridge import GuiControlBridge
from disk_monitor.control_protocol import new_request, success_response


class GuiControlBridgeTests(unittest.TestCase):
    def test_timed_out_request_is_not_executed_later(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = GuiControlBridge(
                temp_dir,
                logger=logging.getLogger(__name__),
                ui_timeout=0.01,
            )
            request = new_request("snapshot.save", {"note": "迟到请求"})

            response = bridge._submit(request)
            handled: list[dict] = []
            bridge.drain(
                lambda item: handled.append(item)
                or success_response(item["request_id"])
            )

            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "timeout")
            self.assertEqual(handled, [])


if __name__ == "__main__":
    unittest.main()
