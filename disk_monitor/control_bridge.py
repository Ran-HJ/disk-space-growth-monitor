from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .control_protocol import error_response
from .control_transport import ControlServer


@dataclass
class PendingControlRequest:
    request: dict[str, Any]
    completed: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class GuiControlBridge:
    """Move authenticated pipe requests onto the Tk main thread."""

    def __init__(
        self,
        control_directory: str | Path,
        *,
        logger: logging.Logger,
        ui_timeout: float = 4.0,
    ) -> None:
        self.requests: queue.Queue[PendingControlRequest] = queue.Queue()
        self.ui_timeout = ui_timeout
        self.server = ControlServer(
            control_directory,
            self._submit,
            logger=logger,
        )

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        while True:
            try:
                pending = self.requests.get_nowait()
            except queue.Empty:
                break
            pending.response = error_response(
                pending.request.get("request_id", ""),
                "gui_unavailable",
                "GUI 控制服务正在关闭",
            )
            pending.completed.set()
        self.server.stop()

    def _submit(self, request: dict[str, Any]) -> dict[str, Any]:
        pending = PendingControlRequest(request)
        self.requests.put(pending)
        if not pending.completed.wait(self.ui_timeout):
            pending.cancelled.set()
            return error_response(
                request["request_id"],
                "timeout",
                "GUI 主线程未及时处理控制请求",
            )
        if pending.response is None:
            return error_response(
                request["request_id"],
                "internal_error",
                "GUI 控制请求未生成响应",
            )
        return pending.response

    def drain(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        limit: int = 20,
    ) -> None:
        for _ in range(limit):
            try:
                pending = self.requests.get_nowait()
            except queue.Empty:
                return
            if pending.cancelled.is_set():
                pending.completed.set()
                continue
            try:
                pending.response = handler(pending.request)
            except Exception:
                pending.response = error_response(
                    pending.request.get("request_id", ""),
                    "internal_error",
                    "GUI 控制请求处理失败",
                )
            finally:
                pending.completed.set()
