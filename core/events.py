"""Шина событий между ядром и интерфейсом.

Ядро не знает об интерфейсе: оно публикует события, а мост (``bridge``) на
них подписывается и пересылает окну. Всё происходит в одном asyncio-лупе,
поэтому обработчики могут напрямую менять состояние экранов - отдельная
синхронизация не нужна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from storage.db import local_time

log = logging.getLogger("aiorc.events")


class EventType(str, Enum):
    """Типы событий выполнения."""

    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_STOPPED = "run_stopped"

    AGENT_STATUS = "agent_status"        # агент сменил статус
    AGENT_THINKING = "agent_thinking"    # шаг рассуждения
    AGENT_DELTA = "agent_delta"          # очередной фрагмент текста модели (стриминг)
    AGENT_TOOL_CALL = "agent_tool_call"  # агент вызвал инструмент
    AGENT_TOOL_RESULT = "agent_tool_result"

    SUBTASK_STARTED = "subtask_started"
    SUBTASK_PROGRESS = "subtask_progress"
    SUBTASK_FINISHED = "subtask_finished"
    SUBTASK_FAILED = "subtask_failed"

    REPORT_CREATED = "report_created"
    REPORT_REVIEWED = "report_reviewed"  # супервайзер вынес вердикт
    SUMMARY_CREATED = "summary_created"
    INCIDENT_CREATED = "incident_created"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"  # пользователь принял решение

    BUDGET_ALERT = "budget_alert"          # расход подошёл к порогу
    BUDGET_EXCEEDED = "budget_exceeded"    # лимит исчерпан, вызовы заблокированы
    BUDGET_EXTENDED = "budget_extended"    # пользователь поднял лимит во время прогона
    USAGE = "usage"                      # расход токенов/денег
    LOG = "log"                          # произвольное сообщение в ленту
    ERROR = "error"


@dataclass
class Event:
    """Одно событие в ленте выполнения."""

    type: EventType
    workspace_id: int | None = None
    task_id: int | None = None
    subtask_id: int | None = None
    agent_id: int | None = None
    agent_name: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def time_short(self) -> str:
        return local_time(self.created_at, "%H:%M:%S")


Handler = Callable[[Event], None]


class EventBus:
    """Публикация событий подписчикам. Сбой обработчика не ломает выполнение."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def emit(self, event: Event) -> None:
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - UI не должен ронять агентов
                log.exception("Обработчик события упал на %s", event.type)

    # Сокращения для частых случаев
    def log(self, message: str, **kwargs: Any) -> None:
        self.emit(Event(EventType.LOG, message=message, **kwargs))

    def error(self, message: str, **kwargs: Any) -> None:
        self.emit(Event(EventType.ERROR, message=message, **kwargs))
