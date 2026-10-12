"""Этап 9 - бюджеты, лимиты и алерты.

Лимит можно поставить на трёх уровнях: весь воркспейс, текущая задача и
отдельный агент. Каждый уровень ограничивается и по токенам, и по деньгам.

Две важные детали реализации:

* Проверка идёт **перед** вызовом модели, а не после. Иначе лимит узнавался бы
  постфактум - деньги уже потрачены, а сказать об этом нечем.
* Фактический расход берётся из ``usage_log``, а не из накопительных счётчиков
  в таблице ``budgets``. Журнал вызовов - единственный источник правды, и при
  перезапуске приложения лимит не «обнуляется» сам собой.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core.events import Event, EventBus, EventType
from storage.repositories import Repos

log = logging.getLogger("aiorc.budget")

# во сколько раз поднимается лимит по решению пользователя
EXTEND_FACTOR = 1.5


class BudgetBlocked(RuntimeError):
    """Вызов модели не выполнен: исчерпан лимит бюджета."""

SCOPE_TITLES = {
    "workspace": "воркспейс",
    "task": "задача",
    "agent": "агент",
}


def money(value: float) -> str:
    """Сумма с точностью, достаточной чтобы не превратиться в «$0.00».

    Лимиты на дешёвых моделях легко оказываются меньше цента, и округление
    до двух знаков сделало бы сообщение бессмысленным.
    """
    if value >= 1:
        return f"${value:,.2f}".replace(",", " ")
    if value >= 0.01:
        return f"${value:.3f}"
    return f"${value:.5f}"


@dataclass
class Limit:
    """Ограничение одного уровня. ``None`` означает «без лимита»."""

    token_limit: int | None = None
    cost_limit: float | None = None
    alert_threshold: float = 0.8

    @property
    def is_set(self) -> bool:
        return bool(self.token_limit) or bool(self.cost_limit)


@dataclass
class ScopeState:
    """Текущее состояние одного уровня бюджета."""

    scope: str
    scope_id: int
    name: str
    limit: Limit = field(default_factory=Limit)
    tokens: int = 0
    cost: float = 0.0
    alerted: bool = False
    exceeded_reported: bool = False

    def ratio(self) -> float:
        """Доля израсходованного - максимум из токенов и денег."""
        parts: list[float] = []
        if self.limit.token_limit:
            parts.append(self.tokens / self.limit.token_limit)
        if self.limit.cost_limit:
            parts.append(self.cost / self.limit.cost_limit)
        return max(parts) if parts else 0.0

    def exceeded(self) -> bool:
        if self.limit.token_limit and self.tokens >= self.limit.token_limit:
            return True
        return bool(self.limit.cost_limit and self.cost >= self.limit.cost_limit)

    def reason(self) -> str:
        """Человеческое объяснение, какой именно лимит упёрся."""
        if self.limit.token_limit and self.tokens >= self.limit.token_limit:
            return (f"{SCOPE_TITLES.get(self.scope, self.scope)} «{self.name}»: "
                    f"израсходовано {self.tokens} токенов из {self.limit.token_limit}")
        if self.limit.cost_limit and self.cost >= self.limit.cost_limit:
            return (f"{SCOPE_TITLES.get(self.scope, self.scope)} «{self.name}»: "
                    f"израсходовано {money(self.cost)} из {money(self.limit.cost_limit)}")
        return ""


class BudgetGuard:
    """Следит за лимитами всех уровней во время прогона.

    Совместим по интерфейсу со старым ``TokenBudget``: ``add`` и ``exhausted``
    вызываются из ``AgentRunner`` так же, как раньше.
    """

    def __init__(self, repos: Repos, bus: EventBus, workspace_id: int,
                 task_id: int | None = None,
                 task_token_limit: int | None = None) -> None:
        self.repos = repos
        self.bus = bus
        self.workspace_id = workspace_id
        self.task_id = task_id
        self._scopes: dict[tuple[str, int], ScopeState] = {}
        # лимит на уровне задачи взят из формы задачи, а не из таблицы budgets
        self._task_limit_from_form = False
        # кто решает, что делать при исчерпании лимита; ``None`` - блокировать
        self.on_blocked: Callable[[ScopeState], Awaitable[bool]] | None = None
        # один вопрос на уровень: параллельные агенты ждут общего ответа
        self._pending: dict[tuple[str, int], asyncio.Future] = {}
        self._load(task_token_limit)

    # загрузка
    def _load(self, task_token_limit: int | None) -> None:
        workspace = self.repos.workspaces.get(self.workspace_id)
        tokens, cost = self.repos.budgets.workspace_totals(self.workspace_id)
        self._scopes[("workspace", self.workspace_id)] = ScopeState(
            "workspace", self.workspace_id,
            workspace.name if workspace else "проект",
            self._limit_of("workspace", self.workspace_id), tokens, cost,
        )

        if self.task_id:
            task = self.repos.tasks.get(self.task_id)
            used_tokens, used_cost = self.repos.budgets.task_totals(self.task_id)
            limit = self._limit_of("task", self.task_id)
            # Лимит, заданный прямо в форме задачи, не должен теряться:
            # если отдельной записи в budgets нет, берём его оттуда.
            if limit.token_limit is None and task_token_limit:
                limit.token_limit = task_token_limit
                self._task_limit_from_form = self.repos.budgets.get(
                    "task", self.task_id) is None
            self._scopes[("task", self.task_id)] = ScopeState(
                "task", self.task_id, task.title if task else "задача",
                limit, used_tokens, used_cost,
            )

        agents = self.repos.agents.list(self.workspace_id)
        limits = self.repos.budgets.list_limits("agent", [a.id for a in agents])
        totals = self.repos.budgets.agent_totals(self.workspace_id)
        for agent in agents:
            row = limits.get(agent.id)
            used = totals.get(agent.id, (0, 0.0))
            self._scopes[("agent", agent.id)] = ScopeState(
                "agent", agent.id, agent.name,
                Limit(row.token_limit, row.cost_limit_usd, row.alert_threshold)
                if row else Limit(),
                used[0], used[1],
            )

    def reload_limits(self) -> None:
        """Перечитывает лимиты из базы, сохраняя накопленный расход.

        Нужно, когда пользователь правит лимиты на странице бюджетов прямо
        во время прогона: иначе новое значение вступило бы в силу только со
        следующим запуском, а до тех пор агенты работали бы по старому.
        """
        for (scope, scope_id), state in self._scopes.items():
            limit = self._limit_of(scope, scope_id)
            if scope == "task" and limit.token_limit is None:
                task = self.repos.tasks.get(scope_id)
                if task is not None and task.token_limit:
                    limit.token_limit = task.token_limit
                    self._task_limit_from_form = self.repos.budgets.get(
                        "task", scope_id) is None
            state.limit = limit
            if not state.exceeded():
                state.exceeded_reported = False
            if state.ratio() < limit.alert_threshold:
                state.alerted = False

    def _limit_of(self, scope: str, scope_id: int) -> Limit:
        row = self.repos.budgets.get(scope, scope_id)
        if row is None:
            return Limit()
        return Limit(row.token_limit, row.cost_limit_usd, row.alert_threshold)

    # проверка
    def blocking_scope(self, agent_id: int | None = None) -> ScopeState | None:
        """Возвращает уровень, лимит которого исчерпан, или ``None``.

        Проверка идёт от общего к частному: сначала воркспейс, потом задача,
        потом конкретный агент - так сообщение получается по самой
        «дорогой» причине.
        """
        for key in (("workspace", self.workspace_id),
                    ("task", self.task_id) if self.task_id else None,
                    ("agent", agent_id) if agent_id else None):
            if key is None:
                continue
            state = self._scopes.get(key)  # type: ignore[arg-type]
            if state is not None and state.exceeded():
                return state
        return None

    def exhausted(self, agent_id: int | None = None) -> bool:
        """Совместимость со старым интерфейсом ``TokenBudget``."""
        return self.blocking_scope(agent_id) is not None

    async def ensure_allowed(self, agent_id: int | None = None) -> ScopeState | None:
        """Проверка перед вызовом модели с возможностью продлить лимит.

        Возвращает ``None``, если вызов разрешён, иначе уровень, который
        его блокирует. Когда назначен ``on_blocked`` (включён
        human-in-the-loop), исчерпанный лимит не обрывает работу сразу:
        пользователя спрашивают, поднять ли лимит. Параллельные агенты,
        упёршиеся в тот же уровень, ждут одного общего ответа, а не
        заваливают человека одинаковыми вопросами.
        """
        while True:
            blocked = self.blocking_scope(agent_id)
            if blocked is None or self.on_blocked is None:
                return blocked
            key = (blocked.scope, blocked.scope_id)
            future = self._pending.get(key)
            if future is None:
                future = asyncio.ensure_future(self._ask_extension(blocked))
                self._pending[key] = future
                future.add_done_callback(lambda _f, k=key: self._pending.pop(k, None))
            if not await asyncio.shield(future):
                return blocked
            # лимит поднят - проверяем все уровни заново: мог упереться другой

    async def _ask_extension(self, state: ScopeState) -> bool:
        assert self.on_blocked is not None
        if not await self.on_blocked(state):
            return False
        self.extend(state)
        return True

    def extend(self, state: ScopeState, factor: float = EXTEND_FACTOR) -> None:
        """Поднимает исчерпанный лимит и сохраняет новое значение."""
        limit = state.limit
        if limit.token_limit:
            limit.token_limit = int(max(limit.token_limit, state.tokens) * factor)
        if limit.cost_limit:
            limit.cost_limit = round(max(limit.cost_limit, state.cost) * factor, 6)
        state.alerted = False
        if state.scope == "task" and self._task_limit_from_form:
            # Лимит задан в форме задачи - там его и обновляем.
            self.repos.tasks.update(state.scope_id, token_limit=limit.token_limit)
        else:
            self.repos.budgets.upsert(state.scope, state.scope_id, limit.token_limit,
                                      limit.cost_limit, limit.alert_threshold)
            self.repos.budgets.sync_used(state.scope, state.scope_id,
                                         state.tokens, state.cost)
        self.bus.emit(Event(
            EventType.BUDGET_EXTENDED, workspace_id=self.workspace_id,
            task_id=self.task_id,
            message=(f"лимит поднят: {SCOPE_TITLES.get(state.scope, state.scope)} "
                     f"«{state.name}» - {self.describe_limit(state)}"),
            payload={"scope": state.scope, "scope_id": state.scope_id},
        ))

    @staticmethod
    def describe_limit(state: ScopeState) -> str:
        parts = []
        if state.limit.token_limit:
            parts.append(f"{state.limit.token_limit} токенов")
        if state.limit.cost_limit:
            parts.append(money(state.limit.cost_limit))
        return ", ".join(parts) or "без лимита"

    # учёт
    def add(self, tokens: int, cost: float, agent_id: int | None = None) -> None:
        """Записывает расход и при необходимости поднимает алерты."""
        keys = [("workspace", self.workspace_id)]
        if self.task_id:
            keys.append(("task", self.task_id))
        if agent_id:
            keys.append(("agent", agent_id))

        for key in keys:
            state = self._scopes.get(key)
            if state is None:
                continue
            state.tokens += tokens
            state.cost += cost
            if state.limit.is_set:
                self.repos.budgets.sync_used(state.scope, state.scope_id,
                                             state.tokens, state.cost)
                self._maybe_alert(state)

    def _maybe_alert(self, state: ScopeState) -> None:
        """Каждый алерт срабатывает один раз на уровень - иначе это шум.

        «Подходим к порогу» и «лимит исчерпан» - разные события, поэтому у
        них отдельные флаги: предупреждение о пороге не должно глушить
        сообщение о превышении, и наоборот.
        """
        if state.exceeded():
            if state.exceeded_reported:
                return
            state.exceeded_reported = True
            state.alerted = True
            self.bus.emit(Event(
                EventType.BUDGET_EXCEEDED, workspace_id=self.workspace_id,
                task_id=self.task_id,
                message=f"лимит исчерпан - {state.reason()}",
                payload={"scope": state.scope, "scope_id": state.scope_id,
                         "ratio": state.ratio()},
            ))
            return

        state.exceeded_reported = False     # после продления лимита снова следим
        if not state.alerted and state.ratio() >= state.limit.alert_threshold:
            state.alerted = True
            percent = state.ratio() * 100
            self.bus.emit(Event(
                EventType.BUDGET_ALERT, workspace_id=self.workspace_id,
                task_id=self.task_id,
                message=(f"бюджет на {percent:.0f}% - "
                         f"{SCOPE_TITLES.get(state.scope, state.scope)} "
                         f"«{state.name}»"),
                payload={"scope": state.scope, "scope_id": state.scope_id,
                         "ratio": state.ratio()},
            ))

    # отчётность
    def snapshot(self) -> list[ScopeState]:
        """Состояние всех уровней - для дашборда и страницы бюджетов."""
        order = {"workspace": 0, "task": 1, "agent": 2}
        return sorted(self._scopes.values(),
                      key=lambda s: (order.get(s.scope, 3), s.name))

    @property
    def tokens(self) -> int:
        state = self._scopes.get(("task", self.task_id)) if self.task_id else None
        if state is None:
            state = self._scopes[("workspace", self.workspace_id)]
        return state.tokens

    @property
    def cost(self) -> float:
        state = self._scopes.get(("task", self.task_id)) if self.task_id else None
        if state is None:
            state = self._scopes[("workspace", self.workspace_id)]
        return state.cost


def load_states(repos: Repos, workspace_id: int) -> list[ScopeState]:
    """Состояние бюджетов вне прогона - для страницы настройки лимитов."""
    task = repos.tasks.current(workspace_id)
    guard = BudgetGuard(repos, EventBus(), workspace_id,
                        task.id if task else None,
                        task.token_limit if task else None)
    return guard.snapshot()
