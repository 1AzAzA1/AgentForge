"""WebSocket-сервер, через который окно Electron разговаривает с ядром.

Протокол (JSON):

* окно -> сервер: ``{"t": "hello", "token": ...}`` первым сообщением, дальше
  ``{"t": "call", "id": 7, "target": "agents", "method": "save", "args": [...]}``;
* сервер -> окно: ``{"t": "snapshot", "state": {...}}`` сразу после входа,
  ``{"t": "batch", "items": [...]}`` с изменениями состояния и событиями,
  ``{"t": "result", "id": 7, "ok": true, "value": ...}`` на каждый вызов.

Сервер слушает только 127.0.0.1. Подключиться может лишь тот, кто знает
токен: его генерирует процесс Electron и передаёт серверу при запуске.
Соединения со страниц сторонних сайтов отсекаются по заголовку Origin.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
from typing import Any
from urllib.parse import urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from bridge.backend import Backend
from bridge.state import error_text

log = logging.getLogger("aiorc.server")

HELLO_TIMEOUT = 5.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def origin_allowed(origin: str | None) -> bool:
    """Окно Electron (file://) и dev-сервер интерфейса на этой машине."""
    if not origin or origin in ("null", "file://"):
        return True
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and (parsed.hostname or "") in LOCAL_HOSTS


def _dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, default=str)


class BridgeServer:
    def __init__(self, backend: Backend, token: str) -> None:
        self.backend = backend
        self.token = token
        self.clients = 0

    async def handler(self, ws: ServerConnection) -> None:
        if not origin_allowed(ws.request.headers.get("Origin") if ws.request else None):
            await ws.close(1008, "origin")
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT))
        except (asyncio.TimeoutError, ConnectionClosed, ValueError, TypeError):
            await ws.close(1008, "hello")
            return
        token = str(hello.get("token", "")) if isinstance(hello, dict) else ""
        if hello.get("t") != "hello" or not hmac.compare_digest(token, self.token):
            await ws.close(1008, "token")
            return

        outbox: asyncio.Queue[str] = asyncio.Queue()
        outbox.put_nowait(_dumps({"t": "snapshot", "state": self.backend.snapshot_all()}))

        def sink(batch: list[dict[str, Any]]) -> None:
            outbox.put_nowait(_dumps({"t": "batch", "items": batch}))

        # Подписка сразу после снимка, без ожиданий между ними: ни одно
        # изменение не потеряется и не придёт раньше снимка.
        hub = self.backend.hub
        hub.add_sink(sink)
        self.clients += 1
        writer = asyncio.create_task(self._writer(ws, outbox))
        calls: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("t") == "call":
                    task = asyncio.create_task(self._call(message, outbox))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
        except ConnectionClosed:
            pass
        finally:
            hub.remove_sink(sink)
            self.clients -= 1
            writer.cancel()

    async def _writer(self, ws: ServerConnection, outbox: asyncio.Queue[str]) -> None:
        try:
            while True:
                await ws.send(await outbox.get())
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    async def _call(self, message: dict[str, Any], outbox: asyncio.Queue[str]) -> None:
        call_id = message.get("id")
        target = self.backend.target(str(message.get("target", "")))
        name = str(message.get("method", ""))
        func = getattr(target, name, None) if target is not None and not name.startswith("_") else None
        if func is None or not getattr(func, "_rpc", False):
            reply = {"t": "result", "id": call_id, "ok": False,
                     "error": f"unknown method {message.get('target')}.{name}"}
        else:
            args = message.get("args") or []
            try:
                value = func(*args)
                if inspect.isawaitable(value):
                    value = await value
                reply = {"t": "result", "id": call_id, "ok": True, "value": value}
            except Exception as exc:  # noqa: BLE001 - ошибка уходит в окно, сервер живёт дальше
                log.exception("Сбой вызова %s.%s", message.get("target"), name)
                reply = {"t": "result", "id": call_id, "ok": False, "error": error_text(exc)}
        # Сначала изменения состояния, которые сделал вызов, потом ответ:
        # окно, получив ответ, уже видит новые данные.
        self.backend.hub.flush()
        outbox.put_nowait(_dumps(reply))


async def run_server(backend: Backend, token: str, port: int = 0,
                     stop: asyncio.Event | None = None,
                     on_ready=None) -> None:
    """Поднимает сервер на 127.0.0.1 и работает, пока не выставят ``stop``."""
    server = BridgeServer(backend, token)
    stop = stop or asyncio.Event()
    async with serve(server.handler, "127.0.0.1", port, max_size=8 * 1024 * 1024) as ws_server:
        actual = ws_server.sockets[0].getsockname()[1] if ws_server.sockets else port
        log.info("Мост слушает 127.0.0.1:%s", actual)
        if on_ready is not None:
            on_ready(actual)
        await stop.wait()
    backend.shutdown()
