"""Общие кирпичики моста: состояние экрана, списки, таймер, форматтеры.

Окно не опрашивает Python. Каждый контроллер хранит свои значения в словаре
``_s``, и любое изменение сразу уходит в ``Hub`` маленьким сообщением: какие
поля поменялись или какая строка списка обновилась. Hub копит сообщения до
конца текущего шага цикла событий и отправляет их одной пачкой, поэтому
десятки событий агентов в секунду не превращаются в десятки отправок.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any, Callable

from app.i18n import tr
from storage.db import local_time

if TYPE_CHECKING:  # pragma: no cover
    from bridge.backend import Backend
    from core.events import Event
    from storage.repositories import Repos

log = logging.getLogger("aiorc.bridge")

_MISSING = object()


def rpc(func: Callable) -> Callable:
    """Помечает метод, который окно может вызвать. Остальные методы закрыты."""
    func._rpc = True  # type: ignore[attr-defined]
    return func


class Hub:
    """Очередь исходящих сообщений с отправкой пачкой."""

    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = []
        self._sinks: list[Callable[[list[dict[str, Any]]], None]] = []
        self._scheduled = False

    def add_sink(self, sink: Callable[[list[dict[str, Any]]], None]) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: Callable[[list[dict[str, Any]]], None]) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def push(self, message: dict[str, Any]) -> None:
        if not self._sinks:
            return
        self._queue.append(message)
        if self._scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        self._scheduled = True
        loop.call_soon(self.flush)

    def flush(self) -> None:
        self._scheduled = False
        if not self._queue:
            return
        batch, self._queue = self._queue, []
        for sink in list(self._sinks):
            try:
                sink(batch)
            except Exception:  # noqa: BLE001 - отвалившееся окно не должно ронять ядро
                log.exception("Не удалось отправить состояние")


class State:
    """Состояние одного экрана: словарь значений и именованные списки."""

    name = ""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._s: dict[str, Any] = {}
        self._lists: dict[str, ListModel] = {}

    def _set(self, **values: Any) -> None:
        changed = {}
        for key, value in values.items():
            if self._s.get(key, _MISSING) != value:
                self._s[key] = value
                changed[key] = value
        if changed:
            self.hub.push({"t": "state", "target": self.name, "values": changed})

    def emit(self, event: str, *args: Any) -> None:
        """Разовое событие для окна (в отличие от состояния, не хранится)."""
        self.hub.push({"t": "event", "target": self.name, "name": event, "args": list(args)})

    def list_model(self, name: str, key: str = "id") -> "ListModel":
        model = ListModel(self, name, key)
        self._lists[name] = model
        return model

    def extras(self) -> dict[str, Any]:
        """Значения, которые зависят от языка: пересчитываются при его смене."""
        return {}

    def publish_extras(self) -> None:
        extras = self.extras()
        if extras:
            self._set(**extras)

    def snapshot(self) -> dict[str, Any]:
        return {"values": dict(self._s),
                "lists": {name: list(m.items) for name, m in self._lists.items()}}


class ListModel:
    """Список словарей с ключом. Окну уходят только изменения."""

    def __init__(self, owner: State, name: str, key: str = "id") -> None:
        self._owner = owner
        self._name = name
        self._key = key
        self._items: list[dict[str, Any]] = []

    def _send(self, op: str, **data: Any) -> None:
        self._owner.hub.push({"t": "list", "target": self._owner.name, "name": self._name,
                              "op": op, **data})

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._items

    @property
    def count(self) -> int:
        return len(self._items)

    def find(self, key_value: Any) -> int:
        for row, item in enumerate(self._items):
            if item.get(self._key) == key_value:
                return row
        return -1

    def get(self, row: int) -> dict[str, Any]:
        return dict(self._items[row]) if 0 <= row < len(self._items) else {}

    def update_row(self, key_value: Any, **changes: Any) -> None:
        """Точечная правка одной строки, без пересылки всего списка."""
        row = self.find(key_value)
        if row < 0:
            return
        item = self._items[row]
        diff = {k: v for k, v in changes.items() if item.get(k, _MISSING) != v}
        if not diff:
            return
        item.update(diff)
        self._send("update", key=key_value, changes=diff)

    def append(self, item: dict[str, Any], limit: int | None = None) -> None:
        """Добавляет строку в конец; при превышении ``limit`` срезает начало."""
        self._items.append(dict(item))
        if limit is not None and len(self._items) > limit:
            del self._items[: len(self._items) - limit]
        self._send("append", item=dict(item), limit=limit)

    def clear(self) -> None:
        if self._items:
            self._items = []
            self._send("set", items=[])

    def set_items(self, items: list[dict[str, Any]]) -> None:
        items = [dict(it) for it in items]
        if items != self._items:
            self._items = items
            # Сообщение уходит в конце шага цикла, а строки до этого могут
            # поправить на месте: окну отдаём копии.
            self._send("set", items=[dict(it) for it in items])


class Timer:
    """Таймер поверх цикла asyncio: одиночный или повторяющийся."""

    def __init__(self, interval_ms: int, callback: Callable[[], None],
                 single_shot: bool = False) -> None:
        self.interval = interval_ms / 1000
        self.callback = callback
        self.single_shot = single_shot
        self._handle: asyncio.TimerHandle | None = None

    def start(self) -> None:
        self.stop()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._handle = loop.call_later(self.interval, self._fire)

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def is_active(self) -> bool:
        return self._handle is not None

    def _fire(self) -> None:
        self._handle = None
        if not self.single_shot:
            self.start()
        try:
            self.callback()
        except Exception:  # noqa: BLE001
            log.exception("Сбой в обработчике таймера")


class Controller(State):
    """Контроллер одной страницы: данные для окна и действия пользователя."""

    # значения экрана до входа в профиль и после выхода из него
    defaults: dict[str, Any] = {}

    def __init__(self, backend: "Backend", name: str) -> None:
        super().__init__(backend.hub)
        self.name = name
        self.backend = backend
        self._set(**copy.deepcopy(self.defaults))

    @property
    def repos(self) -> "Repos":
        assert self.backend.repos is not None, "контроллер вызван до входа в профиль"
        return self.backend.repos

    @property
    def ws_id(self) -> int | None:
        return self.backend.workspace_id

    @property
    def ready(self) -> bool:
        return self.backend.repos is not None

    def toast(self, kind: str, title: str, message: str = "") -> None:
        self.backend.toast(kind, title, message)

    # переопределяемое
    @rpc
    def refresh(self) -> None:
        """Перечитать данные из хранилища."""

    def on_workspace_changed(self) -> None:
        self.refresh()

    def on_event(self, event: "Event") -> None:
        """Реакция на событие ядра (по умолчанию ничего)."""

    def reset(self) -> None:
        """Сброс при выходе из профиля: значения по умолчанию и пустые списки."""
        self._set(**copy.deepcopy(self.defaults))
        for model in self._lists.values():
            model.clear()


# форматтеры


def fmt_tokens(value: int | float) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,}".replace(",", " ")


def fmt_money(value: float) -> str:
    """Сумма с точностью, при которой копеечные расходы не превращаются в $0.00."""
    value = float(value or 0)
    if value >= 100:
        return f"${value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.3f}"
    if value == 0:
        return "$0"
    return f"${value:.4f}"


def when(iso: str | None, fmt: str = "%d.%m %H:%M") -> str:
    return local_time(iso or "", fmt) if iso else ""


def as_int(value: Any, default: int = -1) -> int:
    """Число из окна: вместо него легко приходит null, строка или 3.0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def elide(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_text(exc: BaseException) -> str:
    from providers.base import ProviderError

    if isinstance(exc, (ProviderError, RuntimeError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def status_title(code: str) -> str:
    return tr(f"status.{code}") if code else ""
