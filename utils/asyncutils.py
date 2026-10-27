"""Фоновые задачи в общем цикле asyncio.

В ядре ОДИН asyncio-луп: в нём живут и WebSocket-сервер для окна, и агенты.
Агенты - это корутины/таски в этом лупе, поэтому 15+ параллельных агентов не
превращаются в 15 потоков. Блокирующие вызовы (sqlite, ddgs, чтение файлов)
уводятся в пул потоков через ``asyncio.to_thread``, исполнение кода - в
отдельный процесс.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Awaitable, Callable

log = logging.getLogger("aiorc.async")


def run_async(
    coro: Awaitable[Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> asyncio.Task:
    """Запускает корутину в текущем лупе и передаёт результат колбэкам.

    Колбэки вызываются в том же лупе, поэтому им можно менять состояние экранов.
    """
    task = asyncio.ensure_future(coro)

    def _callback(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("Async task failed: %s\n%s", exc,
                      "".join(traceback.format_exception(exc)))
            if on_error:
                on_error(exc)  # type: ignore[arg-type]
            return
        if on_done:
            on_done(t.result())

    task.add_done_callback(_callback)
    return task


class TaskGroup:
    """Простой учёт фоновых задач воркспейса, чтобы уметь их остановить."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Awaitable[Any], name: str = "") -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def active(self) -> int:
        return sum(1 for t in self._tasks if not t.done())
