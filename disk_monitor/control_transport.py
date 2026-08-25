from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import subprocess
import threading
import uuid
from dataclasses import dataclass
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import Any, Callable

from .control_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    ControlError,
    decode_request,
    decode_response,
    encode_message,
    error_response,
    new_request,
    success_response,
    utc_timestamp,
)


AUTH_KEY_BYTES = 32
ENDPOINT_FILENAME = "control.endpoint.json"
PIPE_PREFIX = r"\\.\pipe\DiskGrowthMonitor-"
MAX_ENDPOINT_BYTES = 8 * 1024
RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ControlEndpoint:
    protocol_version: str
    instance_id: str
    pid: int
    process_started_at: str
    pipe_address: str
    auth_file: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "pipe_address": self.pipe_address,
            "auth_file": self.auth_file,
            "created_at": self.created_at,
        }


def _validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise ControlError("gui_unavailable", "控制服务发现文件无效")
    try:
        int(value, 16)
    except ValueError as error:
        raise ControlError("gui_unavailable", "控制服务实例编号无效") from error
    return value


def _parse_endpoint(data: object) -> ControlEndpoint:
    if not isinstance(data, dict):
        raise ControlError("gui_unavailable", "控制服务发现文件无效")
    instance_id = _validate_instance_id(data.get("instance_id"))
    pipe_address = data.get("pipe_address")
    if not isinstance(pipe_address, str) or not pipe_address.startswith(PIPE_PREFIX):
        raise ControlError("gui_unavailable", "控制服务管道地址无效")
    auth_file = data.get("auth_file")
    expected_auth_file = f"control-{instance_id}.auth"
    if auth_file != expected_auth_file:
        raise ControlError("gui_unavailable", "控制服务认证文件名无效")
    pid = data.get("pid")
    if not isinstance(pid, int) or pid < 1:
        raise ControlError("gui_unavailable", "控制服务进程编号无效")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise ControlError("gui_unavailable", "控制服务协议版本不兼容")
    return ControlEndpoint(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        pid=pid,
        process_started_at=str(data.get("process_started_at", "")),
        pipe_address=pipe_address,
        auth_file=auth_file,
        created_at=str(data.get("created_at", "")),
    )


def _read_limited(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as handle:
            payload = handle.read(maximum + 1)
    except OSError as error:
        raise ControlError("gui_unavailable", "GUI 控制服务未运行") from error
    if len(payload) > maximum:
        raise ControlError("gui_unavailable", "控制服务发现文件过大")
    return payload


def read_control_endpoint(control_directory: str | Path) -> tuple[ControlEndpoint, bytes]:
    directory = Path(control_directory)
    endpoint_payload = _read_limited(directory / ENDPOINT_FILENAME, MAX_ENDPOINT_BYTES)
    try:
        endpoint_data = json.loads(endpoint_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("gui_unavailable", "控制服务发现文件损坏") from error
    endpoint = _parse_endpoint(endpoint_data)
    authkey = _read_limited(directory / endpoint.auth_file, AUTH_KEY_BYTES)
    if len(authkey) != AUTH_KEY_BYTES:
        raise ControlError("unauthorized", "控制服务认证文件无效")
    return endpoint, authkey


def _restrict_auth_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    account = f"{domain}\\{username}" if domain else username
    if not account:
        raise OSError("无法确定当前 Windows 用户，不能保护控制认证文件")
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{account}:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
        ],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if result.returncode != 0:
        raise OSError("无法收紧控制认证文件的 Windows ACL")


def _wait_for_named_pipe(address: str, timeout: float) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
    kernel32.WaitNamedPipeW.restype = ctypes.c_bool
    timeout_ms = max(1, min(int(timeout * 1000), 60_000))
    if not kernel32.WaitNamedPipeW(address, timeout_ms):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "控制管道不可用")


class ControlServer:
    def __init__(
        self,
        control_directory: str | Path,
        request_handler: RequestHandler,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.control_directory = Path(control_directory)
        self.request_handler = request_handler
        self.logger = logger or logging.getLogger(__name__)
        self.instance_id = uuid.uuid4().hex
        self.pipe_address = f"{PIPE_PREFIX}{self.instance_id}"
        self.auth_file = f"control-{self.instance_id}.auth"
        self.authkey = secrets.token_bytes(AUTH_KEY_BYTES)
        self._shutdown_token = secrets.token_hex(32)
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def endpoint_path(self) -> Path:
        return self.control_directory / ENDPOINT_FILENAME

    @property
    def auth_path(self) -> Path:
        return self.control_directory / self.auth_file

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if os.name != "nt":
            raise RuntimeError("Agent 控制服务目前仅支持 Windows")
        self.control_directory.mkdir(parents=True, exist_ok=True)
        for stale_auth in self.control_directory.glob("control-*.auth"):
            stale_auth.unlink(missing_ok=True)
        self._listener = Listener(
            self.pipe_address,
            family="AF_PIPE",
            authkey=self.authkey,
        )
        try:
            self._publish_discovery()
        except Exception:
            self._listener.close()
            self._listener = None
            raise
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="ControlPipeServer",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("control_server_started instance_id=%s", self.instance_id)

    def _publish_discovery(self) -> None:
        auth_temp = self.control_directory / f".{self.auth_file}.tmp"
        endpoint_temp = self.control_directory / f".{ENDPOINT_FILENAME}.tmp"
        try:
            with auth_temp.open("xb") as handle:
                handle.write(self.authkey)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_auth_file(auth_temp)
            os.replace(auth_temp, self.auth_path)
            endpoint = ControlEndpoint(
                protocol_version=PROTOCOL_VERSION,
                instance_id=self.instance_id,
                pid=os.getpid(),
                process_started_at=utc_timestamp(),
                pipe_address=self.pipe_address,
                auth_file=self.auth_file,
                created_at=utc_timestamp(),
            )
            endpoint_payload = json.dumps(
                endpoint.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            with endpoint_temp.open("xb") as handle:
                handle.write(endpoint_payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(endpoint_temp, self.endpoint_path)
        except Exception:
            auth_temp.unlink(missing_ok=True)
            endpoint_temp.unlink(missing_ok=True)
            self.auth_path.unlink(missing_ok=True)
            raise

    def _serve(self) -> None:
        assert self._listener is not None
        while True:
            try:
                connection = self._listener.accept()
            except AuthenticationError:
                self.logger.warning("control_authentication_failed")
                if self._stop_event.is_set():
                    break
                continue
            except OSError:
                if not self._stop_event.is_set():
                    self.logger.exception("control_accept_failed")
                break
            with connection:
                try:
                    if not connection.poll(5.0):
                        continue
                    request = decode_request(
                        connection.recv_bytes(MAX_REQUEST_BYTES)
                    )
                    if (
                        request["command"] == "control.shutdown"
                        and request["args"].get("token") == self._shutdown_token
                    ):
                        response = success_response(request["request_id"])
                    else:
                        response = self.request_handler(request)
                    connection.send_bytes(
                        encode_message(response, maximum=MAX_RESPONSE_BYTES)
                    )
                except ControlError as error:
                    request_id = locals().get("request", {}).get("request_id", "")
                    try:
                        connection.send_bytes(
                            encode_message(
                                error_response(request_id, error.code, error.message),
                                maximum=MAX_RESPONSE_BYTES,
                            )
                        )
                    except OSError:
                        pass
                except (EOFError, OSError):
                    pass
                except Exception:
                    self.logger.exception("control_request_failed")
                    request_id = locals().get("request", {}).get("request_id", "")
                    try:
                        connection.send_bytes(
                            encode_message(
                                error_response(
                                    request_id,
                                    "internal_error",
                                    "GUI 控制请求处理失败",
                                ),
                                maximum=MAX_RESPONSE_BYTES,
                            )
                        )
                    except OSError:
                        pass
            if self._stop_event.is_set():
                break

    def stop(self, timeout: float = 3.0) -> None:
        thread = self._thread
        listener = self._listener
        if thread is not None and thread.is_alive():
            self._stop_event.set()
            try:
                with Client(
                    self.pipe_address,
                    family="AF_PIPE",
                    authkey=self.authkey,
                ) as connection:
                    request = new_request(
                        "control.shutdown",
                        {"token": self._shutdown_token},
                    )
                    connection.send_bytes(
                        encode_message(request, maximum=MAX_REQUEST_BYTES)
                    )
                    if connection.poll(timeout):
                        connection.recv_bytes(MAX_RESPONSE_BYTES)
            except (AuthenticationError, EOFError, OSError):
                pass
            thread.join(timeout)
        if listener is not None:
            listener.close()
        self._cleanup_discovery()
        self._thread = None
        self._listener = None
        self.logger.info("control_server_stopped instance_id=%s", self.instance_id)

    def _cleanup_discovery(self) -> None:
        try:
            endpoint_payload = _read_limited(
                self.endpoint_path, MAX_ENDPOINT_BYTES
            )
            endpoint_data = json.loads(endpoint_payload.decode("utf-8"))
            if endpoint_data.get("instance_id") == self.instance_id:
                self.endpoint_path.unlink(missing_ok=True)
        except (ControlError, UnicodeDecodeError, json.JSONDecodeError, OSError):
            pass
        self.auth_path.unlink(missing_ok=True)


class ControlClient:
    def __init__(
        self,
        control_directory: str | Path,
        *,
        connect_timeout: float = 2.0,
        response_timeout: float = 5.0,
    ) -> None:
        self.control_directory = Path(control_directory)
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout

    def request(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request = new_request(command, args, request_id=request_id)
        try:
            endpoint, authkey = read_control_endpoint(self.control_directory)
            _wait_for_named_pipe(endpoint.pipe_address, self.connect_timeout)
            with Client(
                endpoint.pipe_address,
                family="AF_PIPE",
                authkey=authkey,
            ) as connection:
                connection.send_bytes(
                    encode_message(request, maximum=MAX_REQUEST_BYTES)
                )
                if not connection.poll(self.response_timeout):
                    return error_response(
                        request["request_id"],
                        "timeout",
                        "GUI 控制请求响应超时",
                    )
                return decode_response(
                    connection.recv_bytes(MAX_RESPONSE_BYTES)
                )
        except AuthenticationError:
            return error_response(
                request["request_id"],
                "unauthorized",
                "控制服务认证失败",
            )
        except ControlError as error:
            return error_response(
                request["request_id"], error.code, error.message
            )
        except (EOFError, OSError):
            return error_response(
                request["request_id"],
                "gui_unavailable",
                "GUI 控制服务不可用",
            )
