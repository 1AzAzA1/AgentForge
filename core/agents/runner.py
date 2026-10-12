"""Этап 4 - исполнитель одного агента над одной подзадачей.

Цикл ReAct: модель думает -> при необходимости вызывает инструменты ->
получает их результат -> продолжает. Останов по одному из условий:
собственный сигнал завершения, исчерпание шагов, лимит токенов, отмена.

Контекст агента строится заново для каждой подзадачи, но его личная
история (таблица ``messages``) сохраняется и подмешивается при доработке.
Чужие истории недоступны: выборка всегда идёт по ``agent_id``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config import PATHS
from core.events import Event, EventBus, EventType
from core.tools.base import ToolContext, ToolRegistry, expand_tool_names
from providers.base import ChatMessage, CompletionResult, LLMProvider, ProviderError, ToolCall
from providers.factory import estimate_cost
from storage.models import Agent, Subtask, Task
from storage.repositories import Repos

if TYPE_CHECKING:  # pragma: no cover
    from core.budget import BudgetGuard

log = logging.getLogger("aiorc.runner")

# сколько последних сообщений истории подмешивать без сжатия
HISTORY_WINDOW = 24
# после какого количества символов истории включается сжатие
HISTORY_COMPACT_CHARS = 24_000
# фрагменты стриминга копятся до такой длины, прежде чем уйти в шину:
# отправлять событие на каждый токен бессмысленно дорого
DELTA_FLUSH_CHARS = 16
# статусы HTTP, при которых провайдер, вероятно, просто не умеет стриминг
STREAM_UNSUPPORTED = {400, 404, 405, 415, 422, 501}

FINALIZE_PROMPT = (
    "Лимит шагов на эту подзадачу исчерпан, инструменты больше недоступны. "
    "Подведи итог по тому, что уже успел выяснить: выдай его после строки RESULT: "
    "и укажи строку CONFIDENCE: <0..1>. Честно отметь, что осталось непроверенным."
)


class RunCancelled(Exception):
    """Выполнение остановлено пользователем."""


class BudgetExceeded(Exception):
    """Достигнут лимит токенов задачи."""


@dataclass
class RunResult:
    """Итог работы агента над подзадачей."""

    ok: bool
    result_text: str = ""
    confidence: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    error: str = ""
    tool_calls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Разбор ответа агента
# ---------------------------------------------------------------------------

_CONFIDENCE_RE = re.compile(r"CONFIDENCE\s*[:=]\s*([01](?:[.,]\d+)?)", re.IGNORECASE)
_RESULT_RE = re.compile(r"RESULT\s*:\s*\n?(.+)\Z", re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"\b(TASK_COMPLETE|ЗАДАЧА_ВЫПОЛНЕНА)\b", re.IGNORECASE)


def parse_confidence(text: str) -> float | None:
    """Достаёт самооценку уверенности из ответа (строка ``CONFIDENCE: 0.8``)."""
    match = _CONFIDENCE_RE.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
        return min(max(value, 0.0), 1.0)
    except ValueError:
        return None


def parse_result(text: str) -> str:
    """Возвращает содержимое блока ``RESULT:`` либо весь текст."""
    match = _RESULT_RE.search(text or "")
    body = match.group(1) if match else (text or "")
    return _CONFIDENCE_RE.sub("", body).strip()


def looks_done(text: str) -> bool:
    """Агент явно обозначил завершение подзадачи."""
    return bool(_DONE_RE.search(text or "") or _RESULT_RE.search(text or ""))


# ---------------------------------------------------------------------------


class AgentRunner:
    """Выполняет одну подзадачу силами одного агента."""

    def __init__(self, repos: Repos, bus: EventBus, registry: ToolRegistry,
                 workspace_id: int, workspace_settings: dict,
                 task: Task, subtask: Subtask, agent: Agent,
                 provider: LLMProvider,
                 budget_guard: "BudgetGuard | None" = None,
                 rework_notes: str = "") -> None:
        self.repos = repos
        self.bus = bus
        self.registry = registry
        self.workspace_id = workspace_id
        self.settings = workspace_settings
        self.task = task
        self.subtask = subtask
        self.agent = agent
        self.provider = provider
        self.budget = budget_guard
        self.rework_notes = rework_notes

        self.max_steps = int(workspace_settings.get("agent_max_steps", 10))
        # Шаблоны и старые настройки хранят групповые имена ("files"),
        # поэтому обе стороны разворачиваются до реальных инструментов.
        # Пустой список значит "все инструменты выключены", а не "все включены".
        raw_enabled = workspace_settings.get("tools_enabled")
        enabled = (expand_tool_names(raw_enabled) if raw_enabled is not None
                   else registry.names())
        self.tools_allowed = [
            name for name in expand_tool_names(agent.tools)
            if name in registry.names() and name in enabled
        ]

    # контекст
    def _tool_context(self) -> ToolContext:
        """Границы прав агента на этот запуск."""
        from pathlib import Path

        extra = [Path(p) for p in (self.settings.get("extra_allowed_paths") or [])]
        return ToolContext(
            workspace_dir=PATHS.workspace_dir(self.workspace_id),
            agent_id=self.agent.id,
            subtask_id=self.subtask.id,
            extra_allowed_paths=extra,
            sandbox_backend=self.settings.get("sandbox_backend", "auto"),
            sandbox_timeout_sec=int(self.settings.get("sandbox_timeout_sec", 30)),
            sandbox_memory_mb=int(self.settings.get("sandbox_memory_mb", 512)),
            allow_network_in_sandbox=False,
            search_backend=self.settings.get("search_backend", "duckduckgo"),
            # Ключ поискового API хранится в настройках зашифрованным.
            search_api_key=self.repos.secrets.open(self.settings.get("search_api_key", "")),
            fetch_pages=bool(self.settings.get("fetch_pages", True)),
        )

    def _briefing(self) -> str:
        """Задание агенту: общая задача, своя подзадача, вход от предшественников."""
        parts = [
            "ОБЩАЯ ЗАДАЧА ПРОЕКТА",
            f"{self.task.title}\n{self.task.description}",
            "",
            "ТВОЯ ПОДЗАДАЧА",
            f"{self.subtask.title}",
        ]
        if self.subtask.description:
            parts.append(self.subtask.description)

        # Результаты подзадач, от которых зависит текущая: это не "чужая
        # переписка", а переданный по конвейеру артефакт работы.
        inputs = self._dependency_inputs()
        if inputs:
            parts += ["", "ИСХОДНЫЕ МАТЕРИАЛЫ (результаты предыдущих этапов)", inputs]

        summary = self._latest_summary()
        if summary:
            parts += [
                "",
                "АНОНИМНАЯ СВОДКА ПО ПРОЕКТУ",
                "(источник не указан намеренно - оценивай содержание, а не авторитет)",
                summary,
            ]

        if self.rework_notes:
            parts += ["", "ЗАМЕЧАНИЯ К ПРЕДЫДУЩЕЙ ВЕРСИИ (исправь их)", self.rework_notes]

        if self.tools_allowed:
            parts += ["", "Доступные инструменты: " + ", ".join(self.tools_allowed)]

        parts += [
            "",
            "Когда подзадача выполнена, выдай итог после строки RESULT: "
            "и укажи строку CONFIDENCE: <0..1>.",
        ]
        return "\n".join(parts)

    def _dependency_inputs(self) -> str:
        """Собирает результаты подзадач-предшественников."""
        raw = (self.subtask.depends_on or "").strip()
        if not raw:
            return ""
        chunks: list[str] = []
        for token in raw.split(","):
            token = token.strip()
            if not token.isdigit():
                continue
            dep = self.repos.tasks.get_subtask(int(token))
            if dep and dep.result:
                chunks.append(f"- {dep.title}:\n{dep.result[:4000]}")
        return "\n\n".join(chunks)

    def _latest_summary(self) -> str:
        summaries = self.repos.reports.list_summaries(self.workspace_id, limit=1)
        return summaries[0].content if summaries else ""

    def _history(self) -> list[ChatMessage]:
        """Личная история агента по этой подзадаче, при необходимости сжатая."""
        rows = self.repos.messages.history(self.agent.id, self.subtask.id, limit=200)
        if not rows:
            return []
        total = sum(len(r["content"] or "") for r in rows)
        if total > HISTORY_COMPACT_CHARS or len(rows) > HISTORY_WINDOW:
            head, tail = rows[:2], rows[-HISTORY_WINDOW:]
            dropped = len(rows) - len(head) - len(tail)
            rows = head + ([{
                "role": "user",
                "content": f"[…пропущено {dropped} промежуточных шагов работы…]",
                "tool_call_id": "", "tool_name": "",
            }] if dropped > 0 else []) + tail
        return [
            ChatMessage(role=r["role"], content=r["content"] or "",
                        tool_call_id=r.get("tool_call_id", "") or "",
                        name=r.get("tool_name", "") or "")
            for r in rows
            # tool-сообщения без парного вызова ломают формат части провайдеров
            if r["role"] in ("user", "assistant")
        ]

    # учёт расхода
    def _account(self, result: CompletionResult) -> float:
        """Пишет расход в журнал и в бюджет задачи."""
        cost = estimate_cost(self.agent.provider, self.agent.model,
                             result.usage.input_tokens, result.usage.output_tokens)
        self.repos.budgets.log_call(
            self.workspace_id, self.task.id, self.subtask.id, self.agent.id,
            self.agent.provider, self.agent.model,
            result.usage.input_tokens, result.usage.output_tokens, cost,
        )
        if self.budget:
            self.budget.add(result.usage.total, cost, self.agent.id)
        self.bus.emit(Event(
            EventType.USAGE, workspace_id=self.workspace_id, task_id=self.task.id,
            subtask_id=self.subtask.id, agent_id=self.agent.id,
            agent_name=self.agent.name,
            message=f"+{result.usage.total} токенов (~${cost:.4f})",
            payload={"tokens": result.usage.total, "cost": cost},
        ))
        return cost

    def _emit(self, kind: EventType, message: str, **payload) -> None:
        self.bus.emit(Event(
            kind, workspace_id=self.workspace_id, task_id=self.task.id,
            subtask_id=self.subtask.id, agent_id=self.agent.id,
            agent_name=self.agent.name, message=message, payload=payload,
        ))

    # вызов модели
    async def _check_budget(self) -> str:
        """Проверка ДО вызова модели: узнавать о лимите постфактум бессмысленно.

        Возвращает текст ошибки, если вызов делать нельзя, иначе пустую строку.
        """
        if self.budget is None:
            return ""
        ensure = getattr(self.budget, "ensure_allowed", None)
        blocked = (await ensure(self.agent.id) if ensure is not None
                   else self.budget.blocking_scope(self.agent.id))
        return f"Лимит исчерпан - {blocked.reason()}" if blocked is not None else ""

    async def _call_model(self, messages: list[ChatMessage], step: int,
                          tools: list | None) -> CompletionResult:
        """Один вызов модели со стримингом текста в интерфейс.

        Фрагменты копятся в небольшой буфер и уходят в шину пачками. Если
        провайдер отверг потоковый запрос ещё до первого фрагмента (частая
        история с самописными OpenAI-совместимыми серверами), вызов
        повторяется в обычном режиме, а не роняет подзадачу.
        """
        buffer: dict[str, list[str]] = {"text": [], "reasoning": []}
        streamed = False

        def flush(kind: str) -> None:
            if buffer[kind]:
                chunk = "".join(buffer[kind])
                buffer[kind].clear()
                self._emit(EventType.AGENT_DELTA, chunk, step=step, stream=kind)

        def on_delta(piece: str, kind: str = "text") -> None:
            nonlocal streamed
            streamed = True
            kind = kind if kind in buffer else "text"
            buffer[kind].append(piece)
            if sum(map(len, buffer[kind])) >= DELTA_FLUSH_CHARS or "\n" in piece:
                flush(kind)

        options = dict(temperature=self.agent.temperature,
                       max_tokens=self.agent.max_tokens, tools=tools)
        try:
            result = await self.provider.stream_complete(
                self.agent.model, messages, on_delta=on_delta, **options)
        except ProviderError as exc:
            if streamed or exc.status not in STREAM_UNSUPPORTED:
                raise
            log.info("Стриминг не поддержан (%s), повтор обычным запросом", exc)
            result = await self.provider.complete(self.agent.model, messages, **options)
            if result.text:
                on_delta(result.text, "text")
        finally:
            flush("reasoning")
            flush("text")
        return result

    # основной цикл
    async def run(self) -> RunResult:
        """Прогоняет ReAct-цикл до готового результата или до стоп-условия."""
        tool_ctx = self._tool_context()
        specs = self.registry.specs(self.tools_allowed)

        system_prompt = self.agent.system_prompt.strip() or "Ты - полезный ассистент."
        messages: list[ChatMessage] = [ChatMessage("system", system_prompt)]
        messages += self._history()
        briefing = self._briefing()
        messages.append(ChatMessage("user", briefing))
        self.repos.messages.add(self.agent.id, "user", briefing, self.subtask.id)

        totals = RunResult(ok=False)
        last_text = ""
        last_step_used_tools = False

        # Об ошибке подзадачи сообщает оркестратор по ``RunResult.error``:
        # если сообщать и здесь, в ленте и уведомлениях всё удваивается.
        for step in range(1, self.max_steps + 1):
            blocked = await self._check_budget()
            if blocked:
                totals.error = blocked
                return totals

            totals.steps = step
            self._emit(EventType.AGENT_THINKING, f"шаг {step}/{self.max_steps}", step=step)

            try:
                result = await self._call_model(messages, step, specs or None)
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                totals.error = f"Провайдер: {exc}"
                return totals
            except Exception as exc:  # noqa: BLE001
                log.exception("Сбой вызова модели")
                totals.error = f"{type(exc).__name__}: {exc}"
                return totals

            self._add_usage(totals, result)
            last_text = result.text or last_text

            # Ответ модели сохраняем в её личную историю.
            if result.text:
                self.repos.messages.add(self.agent.id, "assistant", result.text,
                                        self.subtask.id, tokens=result.usage.output_tokens)

            if not result.tool_calls:
                last_step_used_tools = False
                if looks_done(result.text) or (step == self.max_steps and last_text):
                    # Пустой последний ответ не затирает то, что модель
                    # сказала шагом раньше.
                    return self._finish(totals, result.text or last_text)
                if result.text:
                    # Пустое сообщение ассистента часть провайдеров отвергает.
                    messages.append(ChatMessage("assistant", result.text))
                # Модель не обозначила финал - просим завершить.
                nudge = ("Если подзадача выполнена - выдай итог после строки RESULT: "
                         "и строку CONFIDENCE. Если нет - продолжай работу.")
                messages.append(ChatMessage("user", nudge))
                continue

            # есть вызовы инструментов
            last_step_used_tools = True
            messages.append(ChatMessage("assistant", result.text,
                                        tool_calls=result.tool_calls))
            for call in result.tool_calls:
                output = await self._invoke_tool(call, tool_ctx, totals)
                messages.append(ChatMessage("tool", output, tool_call_id=call.id,
                                            name=call.name))
                self.repos.messages.add(self.agent.id, "tool", output[:20000],
                                        self.subtask.id, tool_name=call.name,
                                        tool_call_id=call.id)

        if last_step_used_tools:
            # Последний шаг ушёл на инструменты, итога модель не дала. Выдать
            # промежуточное "сейчас посчитаю" за результат нельзя - просим
            # подвести итог одним дополнительным вызовом без инструментов.
            finalized = await self._finalize(messages, totals)
            if finalized is not None:
                return finalized

        totals.ok = bool(last_text)
        totals.result_text = parse_result(last_text)
        totals.confidence = parse_confidence(last_text)
        if not totals.ok:
            totals.error = "Агент не выдал результат за отведённое число шагов"
        return totals

    def _add_usage(self, totals: RunResult, result: CompletionResult) -> None:
        totals.tokens_in += result.usage.input_tokens
        totals.tokens_out += result.usage.output_tokens
        totals.cost_usd += self._account(result)

    def _finish(self, totals: RunResult, text: str) -> RunResult:
        totals.ok = True
        totals.result_text = parse_result(text)
        totals.confidence = parse_confidence(text)
        self._emit(EventType.SUBTASK_PROGRESS, "получен результат")
        return totals

    async def _finalize(self, messages: list[ChatMessage],
                        totals: RunResult) -> RunResult | None:
        """Дополнительный вызов для итога, когда шаги кончились на инструментах."""
        if await self._check_budget():
            return None
        messages.append(ChatMessage("user", FINALIZE_PROMPT))
        step = totals.steps + 1
        self._emit(EventType.AGENT_THINKING, "подведение итога", step=step)
        try:
            result = await self._call_model(messages, step, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - итог не получился, вернём что было
            log.exception("Не удалось получить итог после исчерпания шагов")
            return None
        self._add_usage(totals, result)
        if not result.text:
            return None
        self.repos.messages.add(self.agent.id, "assistant", result.text,
                                self.subtask.id, tokens=result.usage.output_tokens)
        return self._finish(totals, result.text)

    async def _invoke_tool(self, call: ToolCall, ctx: ToolContext,
                           totals: RunResult) -> str:
        """Вызывает инструмент с проверкой прав и сообщает об этом в ленту."""
        if call.name not in self.tools_allowed:
            return (f"ОШИБКА: инструмент «{call.name}» не разрешён этому агенту. "
                    f"Доступны: {', '.join(self.tools_allowed) or 'нет'}")
        totals.tool_calls.append(call.name)
        preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in call.arguments.items())
        self._emit(EventType.AGENT_TOOL_CALL, f"{call.name}({preview})", tool=call.name)

        output = await self.registry.invoke(call.name, ctx, **call.arguments)
        self._emit(EventType.AGENT_TOOL_RESULT,
                   f"{call.name} → {output[:120].replace(chr(10), ' ')}", tool=call.name)
        return output


class TokenBudget:
    """Простой счётчик на один лимит.

    Оставлен как запасной вариант и для тестов: интерфейс совпадает с
    ``BudgetGuard`` (``add`` / ``exhausted`` / ``blocking_scope``), поэтому
    их можно подставлять друг вместо друга.
    """

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.tokens = 0
        self.cost = 0.0

    def add(self, tokens: int, cost: float, agent_id: int | None = None) -> None:
        self.tokens += tokens
        self.cost += cost

    def exhausted(self, agent_id: int | None = None) -> bool:
        return self.limit is not None and self.tokens >= self.limit

    def blocking_scope(self, agent_id: int | None = None):
        """Возвращает объект с объяснением, чтобы сообщение было единообразным."""
        if not self.exhausted():
            return None
        from core.budget import Limit, ScopeState

        return ScopeState("task", 0, "задача", Limit(self.limit),
                          self.tokens, self.cost)

    def ratio(self) -> float:
        return (self.tokens / self.limit) if self.limit else 0.0
