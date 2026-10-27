"""Подменные провайдеры и каркас сценариев для тестов ядра.

Общие для ``tests/smoke.py`` и pytest-тестов. Модуль не трогает переменные
окружения: каталог данных задаёт тот, кто запускает тесты.
"""

from __future__ import annotations

import asyncio
import itertools

import core.orchestrator as orchestrator_module
from app.config import DEFAULT_WORKSPACE_SETTINGS
from core.hitl import Decision
from core.orchestrator import Orchestrator
from core.supervisor.supervisor import Supervisor, SupervisorModel
from providers.base import CompletionResult, LLMProvider, ToolCall, Usage
from storage.db import Database
from storage.repositories import Repos, UserRepo

_counter = itertools.count()


class Worker(LLMProvider):
    """Исполнитель: при наличии инструментов сначала вызывает один из них."""

    def __init__(self, name: str = "agent", confidence: str = "0.9",
                 use_tool: bool = False, tokens: tuple[int, int] = (200, 80),
                 delay: float = 0.0, always_tool: bool = False) -> None:
        super().__init__()
        self.name = name
        self.confidence = confidence
        self.use_tool = use_tool
        self.always_tool = always_tool
        self.tokens = tokens
        self.delay = delay
        self.calls = 0
        self.tool_rounds = 0

    async def list_models(self) -> list[str]:
        return ["fake-model"]

    async def aclose(self) -> None:
        return None

    async def complete(self, model, messages, *, temperature=0.7,
                       max_tokens=2048, tools=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if tools and (self.always_tool or (self.use_tool and self.calls == 1)):
            self.tool_rounds += 1
            return CompletionResult(
                text="Посчитаю в песочнице.",
                tool_calls=[ToolCall(f"call-{self.calls}", "code_exec",
                                     {"code": "print(6 * 7)", "language": "python"})],
                usage=Usage(*self.tokens),
            )
        return CompletionResult(
            text=(f"Готово.\nCONFIDENCE: {self.confidence}\n"
                  f"RESULT:\nРезультат от {self.name}, попытка {self.calls}."),
            usage=Usage(*self.tokens),
        )


class StreamingWorker(LLMProvider):
    """Исполнитель, который "печатает" ответ по кусочкам, как настоящий стриминг.

    На первом шаге думает вслух и зовёт песочницу, на втором пишет отчёт.
    """

    def __init__(self, name: str, confidence: str = "0.86", delay: float = 0.01) -> None:
        super().__init__()
        self.name = name
        self.confidence = confidence
        self.delay = delay
        self.calls = 0

    async def list_models(self) -> list[str]:
        return ["demo-model"]

    async def aclose(self) -> None:
        return None

    async def complete(self, model, messages, *, temperature=0.7, max_tokens=2048, tools=None):
        return await self.stream_complete(model, messages, tools=tools)

    async def stream_complete(self, model, messages, *, temperature=0.7, max_tokens=2048,
                              tools=None, on_delta=None):
        self.calls += 1
        if self.calls == 1 and tools:
            thought = f"{self.name}: сначала соберу данные по теме, затем проверю цифры.\n"
            for part in thought.split(" "):
                if on_delta:
                    on_delta(part + " ", "reasoning")
                await asyncio.sleep(self.delay)
            return CompletionResult(
                text="Проверю расчёт в песочнице.",
                tool_calls=[ToolCall("c1", "code_exec", {"code": "print(21 * 2)"})],
                usage=Usage(420, 60))
        text = (f"Итоги работы {self.name}. Сравнил три источника, расхождений в ключевых "
                f"цифрах нет; оценка рынка подтверждена расчётом.\n"
                f"CONFIDENCE: {self.confidence}\nRESULT:\nКраткий вывод {self.name}: рынок "
                f"растёт на 18% в год, основные риски связаны с регулированием.")
        for i in range(0, len(text), 9):
            if on_delta:
                on_delta(text[i:i + 9], "text")
            await asyncio.sleep(self.delay)
        return CompletionResult(text=text, usage=Usage(900, 180))


class SupervisorProvider(LLMProvider):
    """Проверяющий: вердикты задаются списком, остальное - заглушки."""

    def __init__(self, verdicts: list[str] | None = None,
                 conflicts: str = '{"conflicts": []}',
                 tokens: tuple[int, int] = (150, 20), fail_reviews: bool = False) -> None:
        super().__init__()
        self.verdicts = verdicts or []
        self.conflicts = conflicts
        self.tokens = tokens
        self.fail_reviews = fail_reviews
        self.reviews = 0
        self.calls = 0

    async def list_models(self) -> list[str]:
        return ["fake-model"]

    async def aclose(self) -> None:
        return None

    async def complete(self, model, messages, **kwargs):
        from providers.base import ProviderError

        self.calls += 1
        system, user = messages[0].content, messages[1].content
        if "ПРОВЕРЯЕМАЯ ПОДЗАДАЧА" in user:
            self.reviews += 1
            if self.fail_reviews:
                raise ProviderError("503: сервис проверки недоступен", 503)
            if self.reviews <= len(self.verdicts):
                return CompletionResult(text=self.verdicts[self.reviews - 1],
                                        usage=Usage(*self.tokens))
            return CompletionResult(text='{"verdict":"ok","notes":"","issues":[]}',
                                    usage=Usage(*self.tokens))
        if "Сравни результаты" in system:
            return CompletionResult(text=self.conflicts, usage=Usage(*self.tokens))
        return CompletionResult(
            text=("ФАКТЫ:\nработа идёт\nРАСХОЖДЕНИЯ:\nнет\nОТКРЫТЫЕ ВОПРОСЫ:\nнет"),
            usage=Usage(*self.tokens),
        )


def patch_supervisor(provider: SupervisorProvider) -> None:
    """Подменяет модель супервайзера, не трогая остальную его логику."""

    class Patched(Supervisor):
        def _resolve_model(self):
            if self._model is None:
                self._model = SupervisorModel(provider, "fake-model", "openai",
                                              "api", "тестовый супервайзер")
            return self._model

    orchestrator_module.Supervisor = Patched


def build_project(subtask_count: int = 2, agent_count: int = 2,
                  settings: dict | None = None, token_limit: int | None = None):
    """Создаёт профиль, воркспейс, агентов и задачу."""
    db = Database()
    session = UserRepo(db).create(f"smoke{next(_counter)}", "password123")
    repos = Repos(db, session)

    config = dict(DEFAULT_WORKSPACE_SETTINGS)
    config.update(summary_interval_minutes=0, human_in_the_loop=False,
                  max_rework_rounds=1)
    config.update(settings or {})

    workspace = repos.workspaces.create(session.user_id, "Смоук-проект", "", config)
    key = repos.keys.create("Тестовый ключ", "openai", "sk-secret-value", "")

    agents = [
        repos.agents.create(workspace.id, f"Агент {i + 1}", "analyst",
                            "Ты исполнитель.", key.id, "openai", "gpt-4o-mini",
                            {"temperature": 0.3, "max_tokens": 512,
                             "tools": ["code_exec"]})
        for i in range(agent_count)
    ]
    supervisor = repos.agents.create(workspace.id, "Супервайзер", "supervisor",
                                     "Ты проверяющий.", key.id, "openai",
                                     "gpt-4o-mini", {}, is_supervisor=True)
    repos.workspaces.update(workspace.id,
                            settings={**config, "supervisor_agent_id": supervisor.id})

    task = repos.tasks.create(workspace.id, "Смоук-задача",
                              "Проверить работу конвейера", "auto", token_limit)
    for i in range(subtask_count):
        repos.tasks.add_subtask(task.id, f"Подзадача {i + 1}", "",
                                agents[i % len(agents)].id)
    return repos, workspace, task, agents, key


async def drive(orch: Orchestrator, workspace_id: int, task_id: int,
                answers: list[tuple[Decision, str]] | None = None,
                fallback: Decision = Decision.APPROVE, timeout: float = 30.0):
    """Запускает прогон, отвечая на вопросы human-in-the-loop по сценарию."""
    plan = list(answers or [])

    async def responder() -> None:
        while True:
            gate = orch.gate
            if gate and gate.pending():
                request = gate.pending()[0]
                decision, comment = plan.pop(0) if plan else (fallback, "авто")
                if decision not in request.options:
                    decision = request.options[0]
                gate.resolve(request.id, decision, comment)
            await asyncio.sleep(0.02)

    helper = asyncio.ensure_future(responder())
    try:
        return await asyncio.wait_for(orch.run_task(workspace_id, task_id), timeout)
    finally:
        helper.cancel()
