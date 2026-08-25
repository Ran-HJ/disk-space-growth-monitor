from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


PROTOCOL_VERSION = "1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ControlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def new_request(
    command: str,
    args: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id or uuid.uuid4().hex,
        "timestamp": utc_timestamp(),
        "command": command,
        "args": args or {},
    }


def success_response(
    request_id: str,
    *,
    data: dict[str, Any] | list[Any] | None = None,
    code: str = "ok",
    message: str = "",
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": True,
        "code": code,
        "message": message,
        "request_id": request_id,
        "timestamp": utc_timestamp(),
        "data": data if data is not None else {},
    }


def error_response(
    request_id: str,
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": False,
        "code": code,
        "message": message,
        "request_id": request_id,
        "timestamp": utc_timestamp(),
        "data": data or {},
    }


def encode_message(payload: dict[str, Any], *, maximum: int) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ControlError("invalid_args", "控制消息超过允许长度")
    return encoded


def decode_request(payload: bytes) -> dict[str, Any]:
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("invalid_args", "控制请求不是有效的 UTF-8 JSON") from error
    if not isinstance(request, dict):
        raise ControlError("invalid_args", "控制请求必须是 JSON 对象")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ControlError("invalid_args", "不支持的控制协议版本")
    if not isinstance(request.get("request_id"), str) or not request["request_id"]:
        raise ControlError("invalid_args", "缺少 request_id")
    if not isinstance(request.get("command"), str) or not request["command"]:
        raise ControlError("invalid_args", "缺少 command")
    if not isinstance(request.get("args", {}), dict):
        raise ControlError("invalid_args", "args 必须是 JSON 对象")
    return request


def decode_response(payload: bytes) -> dict[str, Any]:
    try:
        response = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("internal_error", "GUI 返回了无效响应") from error
    if not isinstance(response, dict) or response.get("protocol_version") != PROTOCOL_VERSION:
        raise ControlError("internal_error", "GUI 返回了不支持的响应格式")
    return response
