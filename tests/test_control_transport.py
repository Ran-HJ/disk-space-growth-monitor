from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from disk_monitor.control_protocol import success_response
from disk_monitor.control_transport import (
    AUTH_KEY_BYTES,
    ControlClient,
    ControlServer,
    read_control_endpoint,
)


@unittest.skipUnless(os.name == "nt", "AF_PIPE 仅在 Windows 上可用")
class ControlTransportTests(unittest.TestCase):
    def test_server_start_removes_stale_auth_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            control_directory = Path(temp_dir)
            stale = control_directory / ("control-" + "0" * 32 + ".auth")
            stale.write_bytes(b"stale")
            server = ControlServer(
                control_directory,
                lambda request: success_response(request["request_id"]),
            )
            try:
                server.start()
                self.assertFalse(stale.exists())
                self.assertTrue(server.auth_path.exists())
            finally:
                server.stop()

    def test_server_client_round_trip_publishes_and_cleans_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            control_dir = Path(temp_dir)

            def handle(request):
                return success_response(
                    request["request_id"],
                    data={"echo": request["args"]},
                )

            server = ControlServer(control_dir, handle)
            server.start()
            try:
                endpoint, authkey = read_control_endpoint(control_dir)
                endpoint_text = (control_dir / "control.endpoint.json").read_text(
                    encoding="utf-8"
                )
                self.assertTrue(endpoint.pipe_address.startswith(r"\\.\pipe\DiskGrowthMonitor-"))
                self.assertEqual(len(authkey), AUTH_KEY_BYTES)
                self.assertNotIn(authkey.hex(), endpoint_text)
                self.assertNotIn("authkey", json.loads(endpoint_text))

                response = ControlClient(control_dir).request(
                    "probe.echo", {"text": "中文路径 C:\\测试"}
                )

                self.assertTrue(response["ok"])
                self.assertEqual(response["data"]["echo"]["text"], "中文路径 C:\\测试")
                self.assertTrue(response["timestamp"].endswith("Z"))
            finally:
                auth_path = control_dir / endpoint.auth_file
                server.stop()

            self.assertFalse((control_dir / "control.endpoint.json").exists())
            self.assertFalse(auth_path.exists())

    def test_wrong_authkey_is_rejected_without_stopping_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            control_dir = Path(temp_dir)
            server = ControlServer(
                control_dir,
                lambda request: success_response(request["request_id"]),
            )
            server.start()
            try:
                endpoint, original_key = read_control_endpoint(control_dir)
                auth_path = control_dir / endpoint.auth_file
                auth_path.write_bytes(b"x" * AUTH_KEY_BYTES)

                response = ControlClient(control_dir).request("probe.echo")

                self.assertFalse(response["ok"])
                self.assertEqual(response["code"], "unauthorized")
                auth_path.write_bytes(original_key)
                self.assertTrue(
                    ControlClient(control_dir).request("probe.echo")["ok"]
                )
            finally:
                server.stop()

    def test_stale_endpoint_returns_gui_unavailable_without_long_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            control_dir = Path(temp_dir)
            server = ControlServer(
                control_dir,
                lambda request: success_response(request["request_id"]),
            )
            server.start()
            endpoint_path = control_dir / "control.endpoint.json"
            endpoint_text = endpoint_path.read_text(encoding="utf-8")
            endpoint, authkey = read_control_endpoint(control_dir)
            server.stop()
            (control_dir / endpoint.auth_file).write_bytes(authkey)
            endpoint_path.write_text(endpoint_text, encoding="utf-8")

            response = ControlClient(
                control_dir,
                connect_timeout=0.2,
                response_timeout=0.2,
            ).request("probe.echo")

            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "gui_unavailable")


if __name__ == "__main__":
    unittest.main()
