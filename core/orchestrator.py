"""Этап 4 - оркестратор выполнения задачи.

Отвечает за расписание: какие подзадачи можно запускать сейчас, какие ждут
предшественников, сколько агентов работают параллельно. Каждый агент
выполняет свои подзадачи последовательно (лок на агента), разные агенты -
параллельно, все в одном asyncio-лупе.

Оркестратор ведёт весь жизненный цикл: статусы, отчёты, расход, паузы и
корректную остановку по требованию пользователя.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.config import DEFAULT_WORKSPACE_SETTINGS, PATHS
from core.agents.runner import AgentRunner, RunResult
from core.budget import BudgetGuard, ScopeState
from core.events import Event, EventBus, EventType
from core.hitl import Answer, ApprovalGate, Decision, Reason
from core.supervisor.supervisor import Supervisor
from core.tools.base import ToolRegistry, default_registry
from providers.base import LLMProvider
from providers.factory import build_provider
from storage.models import Agent, Report, Subtask, Task
from storage.repositories import Repos

log = logging.getLogger("aiorc.orchestrator")

# сколько дополнительных кругов доработки может выдать человек
# сверх автоматического лимита супервайзера
USER_REWORK_LIMIT = 3

# сколько агентов могут работать одновременно, если в настройках не указано
DEFAULT_CONCURRENCY = 6


@dataclass
class RunState:
    """Наблюдаемое состояние текущего прогона."""

    running: bool = False
    paused: bool = False
    task_id: int | None = None
    started_at: str = ""
    finished: int = 0
    total: int = 0
    failed: int = 0
    tokens: int = 0
    cost: float = 0.0
    reworks: int = 0
    escalated: int = 0
    errors: list[str] = field(default_factory=list)


class Orchestrator:
    """Запускает подзадачи задачи и собирает отчёты агентов."""

    def __init__(self, repos: Repos, bus: EventBus,
                 registry: ToolRegistry | None = None) -> None:
        self.repos = repos
        self.bus = bus
        self.registry = registry or default_registry()
        self.state = RunState()

        self._pause = asyncio.Event()
        self._pause.set()                       # "не на паузе"
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._agent_locks: dict[int, asyncio.Lock] = {}
        self._providers: dict[int, LLMProvider] = {}
        self._budget: BudgetGuard | None = None
        self._supervisor: Supervisor | None = None
        self._summary_on_event: bool = True
        self._gate: ApprovalGate | None = None
        self._confidence_threshold: float = 0.0
        self._semaphore: asyncio.Semaphore | None = None
        self._workspace_id: int | None = None
        # id подзадач текущей задачи - зависимости на прочие id игнорируются
        self._known_ids: set[int] | None = None

    # управление
    def pause(self) -> None:
        if self.state.running and not self.state.paused:
            self.state.paused = True
            self._pause.clear()
            self.bus.emit(Event(EventType.RUN_PAUSED, task_id=self.state.task_id,
                                workspace_id=self._workspace_id,
                                message="Выполнение поставлено на паузу"))

    def resume(self) -> None:
        if self.state.running and self.state.paused:
            self.state.paused = False
            self._pause.set()
            self.bus.emit(Event(EventType.RUN_RESUMED, task_id=self.state.task_id,
                                workspace_id=self._workspace_id,
                                message="Выполнение возобновлено"))

    def stop(self) -> None:
        if not self.state.running:
            return
        self._stop.set()
        self._pause.set()                       # разбудить ожидающих
        if self._gate is not None:
            self._gate.cancel_all()             # снять висящие вопросы
        for task in list(self._tasks):
            task.cancel()
        self.bus.emit(Event(EventType.RUN_STOPPED, task_id=self.state.task_id,
                            workspace_id=self._workspace_id,
                            message="Остановка по команде пользователя"))

    # основной запуск
    async def run_task(self, workspace_id: int, task_id: int,
                       concurrency: int | None = None) -> RunState:
        """Прогоняет все подзадачи задачи с учётом зависимостей."""
        if self.state.running:
            raise RuntimeError("Выполнение уже запущено")

        workspace = self.repos.workspaces.get(workspace_id)
        task = self.repos.tasks.get(task_id)
        if workspace is None or task is None:
            raise RuntimeError("Воркспейс или задача не найдены")

        settings = {**DEFAULT_WORKSPACE_SETTINGS, **workspace.settings}
        PATHS.workspace_dir(workspace_id).mkdir(parents=True, exist_ok=True)

        subtasks = [s for s in self.repos.tasks.subtasks(task_id) if s.status != "done"]
        unassigned = [s for s in subtasks if not s.agent_id]
        if unassigned:
            raise RuntimeError(
                "Не у всех подзадач назначен исполнитель: "
                + ", ".join(s.title for s in unassigned[:5])
            )
        if not subtasks:
            raise RuntimeError("Нет подзадач для выполнения")

        if concurrency is None:
            try:
                concurrency = int(settings.get("max_parallel_agents") or DEFAULT_CONCURRENCY)
            except (TypeError, ValueError):
                concurrency = DEFAULT_CONCURRENCY

        self._workspace_id = workspace_id
        self._reset_state(task_id, len(subtasks))
        self._start_gate(workspace_id, settings)
        self._budget = BudgetGuard(self.repos, self.bus, workspace_id,
                                   task.id, task.token_limit)
        if self._gate is not None:
            self._budget.on_blocked = self._on_budget_blocked
        self.repos.tasks.update(task_id, status="running")
        self.bus.emit(Event(EventType.RUN_STARTED, workspace_id=workspace_id,
                            task_id=task_id,
                            message=f"Запуск: {len(subtasks)} подзадач"))

        self._start_supervisor(workspace_id, settings, task)

        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        try:
            await self._schedule(workspace_id, settings, task, subtasks)
            if self._supervisor is not None and not self._stop.is_set():
                # Финальный разбор: ищем расхождения между результатами
                # и подводим общий итог для команды. Сбой здесь не должен
                # перечеркнуть уже сделанную работу.
                try:
                    await self._supervisor.find_conflicts(task)
                    await self._supervisor.make_summary(task, trigger="final")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("Сбой финального разбора супервайзера")
                    self.bus.error("финальный разбор супервайзера не выполнен",
                                   workspace_id=workspace_id, task_id=task_id,
                                   agent_name="Супервайзер")
        except asyncio.CancelledError:
            log.info("Прогон отменён")
        finally:
            await self._cleanup()
            self.state.running = False
            self.repos.tasks.update(task_id, status=self._final_status())
            self.bus.emit(Event(
                EventType.RUN_FINISHED, workspace_id=workspace_id, task_id=task_id,
                message=(f"Готово: {self.state.finished} выполнено, "
                         f"{self.state.failed} с ошибкой, "
                         f"{self.state.reworks} доработок, "
                         f"{self.state.escalated} на решение пользователя, "
                         f"{self.state.tokens} токенов, ~${self.state.cost:.4f}"),
                payload={"finished": self.state.finished, "failed": self.state.failed,
                         "reworks": self.state.reworks,
                         "escalated": self.state.escalated,
                         "stopped": self._stop.is_set()},
            ))
        return self.state

    def _final_status(self) -> str:
        if self._stop.is_set():
            return "stopped"
        if self.state.failed:
            return "failed"
        if self.state.escalated:
            return "review"         # есть результаты, которые ждут человека
        return "done"

    def _reset_state(self, task_id: int, total: int) -> None:
        from storage.db import utcnow

        self._stop.clear()
        self._pause.set()
        self._tasks.clear()
        self._agent_locks.clear()
        self.state = RunState(running=True, task_id=task_id, total=total,
                              started_at=utcnow())

    # расписание
    async def _schedule(self, workspace_id: int, settings: dict, task: Task,
                        subtasks: list[Subtask]) -> None:
        """Волнами запускает подзадачи, у которых выполнены зависимости."""
        pending = {s.id: s for s in subtasks}
        all_subtasks = self.repos.tasks.subtasks(task.id)
        done_ids: set[int] = {s.id for s in all_subtasks if s.status == "done"}
        titles = {s.id: s.title for s in all_subtasks}
        # Ссылка на подзадачу, которой в задаче больше нет (удалена, осталась
        # от старой версии), не должна навсегда блокировать зависимую.
        self._known_ids = set(titles)

        while pending and not self._stop.is_set():
            ready = [s for s in pending.values() if self._deps_met(s, done_ids)]
            if not ready:
                self._block_unreachable(workspace_id, task, pending, done_ids, titles)
                break

            wave = [
                asyncio.ensure_future(self._run_subtask(workspace_id, settings, task, s))
                for s in ready
            ]
            self._tasks.update(wave)
            results = await asyncio.gather(*wave, return_exceptions=True)
            self._tasks.difference_update(wave)
            for subtask, outcome in zip(ready, results):
                pending.pop(subtask.id, None)
                # CancelledError наследуется от BaseException, а не от Exception,
                # поэтому проверяем именно BaseException - иначе отменённая
                # подзадача была бы ошибочно засчитана как выполненная.
                if isinstance(outcome, asyncio.CancelledError):
                    continue
                if isinstance(outcome, BaseException):
                    self.state.failed += 1
                    self.state.errors.append(f"{subtask.title}: {outcome}")
                elif outcome:
                    done_ids.add(subtask.id)

            # Завершён этап работ: если пользователь просил останавливаться
            # на контрольных точках, спрашиваем перед следующей волной.
            # На последней волне вопрос не задаём - спрашивать "продолжать?",
            # когда продолжать уже нечего, бессмысленно.
            if (pending and self._gate is not None and not self._stop.is_set()
                    and settings.get("hitl_pause_on_milestone")):
                answer = await self._gate.ask(
                    Reason.MILESTONE,
                    f"Завершён этап: готово {len(done_ids)} из {len(subtasks)} подзадач. "
                    f"Продолжать?",
                    details=self._wave_summary(task, done_ids),
                    task_id=task.id,
                )
                if answer.decision is Decision.ABORT:
                    self.bus.log("Прогон остановлен на контрольной точке",
                                 workspace_id=workspace_id, task_id=task.id)
                    self._stop.set()
                    break

    def _block_unreachable(self, workspace_id: int, task: Task,
                           pending: dict[int, Subtask], done_ids: set[int],
                           titles: dict[int, str]) -> None:
        """Помечает подзадачи, которые уже не смогут стартовать, и объясняет почему.

        Причин две, и путать их нельзя: либо не выполнена одна из
        зависимостей (упала или не принята), либо зависимости замкнуты в
        цикл. Раньше обе выдавались как «невозможно разрешить зависимости»,
        и упавший предшественник выглядел как ошибка в графе.
        """
        for subtask in pending.values():
            missing = [dep for dep in self._deps(subtask) if dep not in done_ids]
            failed_deps = [dep for dep in missing if dep not in pending]
            if failed_deps:
                names = ", ".join(f"«{titles.get(d, d)}»" for d in failed_deps)
                reason = f"не выполнена зависимость {names}"
            else:
                reason = "зависимости замкнуты в цикл"
            self.repos.tasks.update_subtask(subtask.id, status="error")
            self.state.failed += 1
            self.bus.emit(Event(
                EventType.SUBTASK_FAILED, workspace_id=workspace_id, task_id=task.id,
                subtask_id=subtask.id, agent_id=subtask.agent_id,
                message=f"«{subtask.title}» не запущена: {reason}",
            ))

    def _wave_summary(self, task: Task, done_ids: set[int]) -> str:
        """Короткая сводка по завершённой волне - чтобы решать осознанно."""
        lines: list[str] = []
        for subtask in self.repos.tasks.subtasks(task.id):
            if subtask.id not in done_ids:
                continue
            body = (subtask.result or "").strip().replace("\n", " ")
            lines.append(f"· {subtask.title}: {body[:180]}" if body else f"· {subtask.title}")
        tokens, cost = self.repos.budgets.task_totals(task.id)
        lines.append("")
        lines.append(f"Израсходовано по задаче: {tokens} токенов, ~${cost:.4f}")
        return "\n".join(lines)

    def _deps(self, subtask: Subtask) -> list[int]:
        raw = (subtask.depends_on or "").strip()
        deps = [int(t) for t in (tok.strip() for tok in raw.split(",")) if t.isdigit()]
        known = self._known_ids
        return [d for d in deps if known is None or d in known]

    def _deps_met(self, subtask: Subtask, done_ids: set[int]) -> bool:
        return all(dep in done_ids for dep in self._deps(subtask))

    # выполнение одной подзадачи
    async def _run_subtask(self, workspace_id: int, settings: dict, task: Task,
                           subtask: Subtask) -> bool:
        agent = self.repos.agents.get(subtask.agent_id or 0)
        if agent is None or not agent.enabled:
            self._fail(subtask, agent, "Исполнитель недоступен или отключён")
            return False

        # Сначала лок агента, потом слот параллельности. В обратном порядке
        # подзадачи одного агента занимали бы слоты, простаивая в очереди
        # к собственному локу, и другие агенты ждали бы впустую.
        lock = self._agent_locks.setdefault(agent.id, asyncio.Lock())
        assert self._semaphore is not None
        async with lock, self._semaphore:
            await self._pause.wait()
            if self._stop.is_set():
                return False

            provider = self._provider_for(agent)
            if provider is None:
                self._fail(subtask, agent, "У агента не настроен API-ключ или модель")
                return False

            self._set_status(agent, subtask, "running")
            self.bus.emit(Event(
                EventType.SUBTASK_STARTED, workspace_id=workspace_id, task_id=task.id,
                subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
                message=subtask.title,
            ))

            max_rework = int(settings.get("max_rework_rounds", 2))
            notes = self._rework_notes(subtask)

            # Цикл "выполнил -> проверили -> доработал". Лок агента держится
            # всё это время: доработку делает тот же исполнитель, и его
            # личная история остаётся связной.
            #
            # max_rework ограничивает АВТОМАТИЧЕСКИЕ доработки супервайзера.
            # Когда доработку назначает человек, он даёт дополнительный круг
            # сверх лимита: его решение важнее настройки. Жёсткий потолок
            # USER_REWORK_LIMIT защищает от бесконечного цикла, если человек
            # раз за разом возвращает работу.
            attempt = 0
            allowed = max_rework
            user_grants = 0          # сколько кругов уже выдал человек
            while attempt <= allowed:
                runner = AgentRunner(
                    self.repos, self.bus, self.registry, workspace_id, settings,
                    task, subtask, agent, provider, self._budget,
                    rework_notes=notes,
                )
                try:
                    result = await runner.run()
                except asyncio.CancelledError:
                    self._set_status(agent, subtask, "paused")
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("Агент %s упал на подзадаче %s", agent.name, subtask.id)
                    self._fail(subtask, agent, f"{type(exc).__name__}: {exc}")
                    return False

                accepted, notes, granted = await self._review_result(
                    workspace_id, task, subtask, agent, result, attempt, max_rework
                )
                if accepted is not None:
                    return accepted

                # accepted is None -> назначена доработка, идём на новый круг.
                # Потолок считается по числу выданных человеком кругов,
                # а не относительно текущей попытки: иначе граница уезжала бы
                # вперёд на каждом круге и цикл никогда бы не закончился.
                if granted and user_grants < USER_REWORK_LIMIT:
                    user_grants += 1
                    allowed = attempt + 1
                attempt += 1
                subtask = self.repos.tasks.get_subtask(subtask.id) or subtask
                await self._pause.wait()
                if self._stop.is_set():
                    return False

            # Сюда попадаем, когда человек снова вернул работу, а его круги
            # доработки уже исчерпаны. Подзадача не должна остаться висеть
            # "на доработке": прогон считал бы её не ошибкой, а ничем.
            self._fail(subtask, agent, f"исчерпан лимит доработок по «{subtask.title}»")
            self.repos.agents.set_status(agent.id, "idle")
            return False

    async def _ask_human(self, reason: Reason, question: str, **kwargs) -> Answer:
        """Вопрос человеку изнутри подзадачи.

        Пока человек думает, агент не работает, поэтому его слот
        параллельности отдаётся другим подзадачам и забирается обратно
        после ответа. Лок агента при этом держится: к ответу он вернётся
        со связной историей.
        """
        assert self._gate is not None and self._semaphore is not None
        self._semaphore.release()
        try:
            return await self._gate.ask(reason, question, **kwargs)
        finally:
            await self._semaphore.acquire()

    async def _review_result(self, workspace_id: int, task: Task, subtask: Subtask,
                             agent: Agent, result: RunResult,
                             attempt: int, max_rework: int
                             ) -> tuple[bool | None, str, bool]:
        """Сохраняет результат и проводит его через супервайзера.

        Возвращает ``(итог, замечания, доработку назначил человек)``:
        итог ``True``/``False`` - подзадача закрыта успешно или с ошибкой,
        ``None`` - назначена доработка. Третий флаг говорит вызывающему коду,
        что круг доработки нужно выдать сверх автоматического лимита.
        """
        finished = self._finish_subtask(workspace_id, task, subtask, agent, result)
        if not finished:
            return False, "", False

        supervisor = self._supervisor
        report = self._last_report(subtask.id) if supervisor is not None else None
        if supervisor is None or report is None:
            # Без супервайзера отчёт принимается как есть: пользователь сам
            # отказался от проверки, и в ленте об этом написано при старте.
            self.repos.tasks.update_subtask(subtask.id, status="done")
            self.state.finished += 1
            return True, "", False

        verdict = await supervisor.review(task, subtask, report)

        if verdict.verdict == "unverified":
            return await self._handle_unverified(workspace_id, task, subtask, agent,
                                                 report, verdict)

        if verdict.accepted:
            self.repos.tasks.update_subtask(subtask.id, status="done")
            self.state.finished += 1
            # Замечания по подзадаче закрываем: результат принят, и открытый
            # инцидент без причины висел бы на дашборде как "ждёт решения".
            # Это и замечания, из-за которых работа уходила на доработку, и
            # мелкие пометки, которые супервайзер оставил, принимая отчёт.
            reworked = bool(subtask.rework_count or attempt > 0)
            closed = self.repos.incidents.resolve_for_subtask(
                subtask.id,
                "Исправлено при доработке, результат принят" if reworked
                else "Результат принят супервайзером, замечание некритично",
            )
            if closed and reworked:
                self.bus.log(f"закрыто замечаний после доработки: {closed}",
                             workspace_id=workspace_id, subtask_id=subtask.id,
                             agent_name="Супервайзер")
            # Супервайзер доволен, но сам исполнитель - нет. Это как раз тот
            # случай, когда дешевле спросить человека, чем нести сомнительный
            # результат дальше по цепочке подзадач.
            if (self._gate is not None and report.confidence is not None
                    and report.confidence < self._confidence_threshold):
                self._set_status(agent, subtask, "paused")
                answer = await self._ask_human(
                    Reason.LOW_CONFIDENCE,
                    f"«{subtask.title}»: исполнитель оценил свою уверенность "
                    f"в {report.confidence:.2f}",
                    details=self._decision_details(subtask, report, verdict),
                    task_id=task.id, subtask_id=subtask.id, agent_name=agent.name,
                )
                if answer.decision is not Decision.APPROVE:
                    # Решение отменяет уже засчитанную приёмку.
                    self.state.finished -= 1
                    return await self._apply_decision(
                        workspace_id, task, subtask, agent, answer, None
                    )
                # Пользователь подтвердил результат - возвращаем статусы,
                # которые были сняты на время ожидания ответа.
                self.repos.tasks.update_subtask(subtask.id, status="done")
                self.repos.agents.set_status(agent.id, "idle")

            await self._maybe_summarize(task)
            return True, "", False

        can_rework = attempt < max_rework and verdict.verdict == "rework"
        if can_rework:
            self.state.reworks += 1
            self.repos.tasks.update_subtask(
                subtask.id, status="rework", rework_count=subtask.rework_count + 1
            )
            self.bus.emit(Event(
                EventType.SUBTASK_PROGRESS, workspace_id=workspace_id, task_id=task.id,
                subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
                message=f"доработка {attempt + 1}/{max_rework}: "
                        f"{verdict.notes[:120] or 'см. замечания супервайзера'}",
            ))
            return None, verdict.notes, False

        # Доработки исчерпаны либо это конфликт - фиксируем инцидент
        # и, если human-in-the-loop включён, останавливаемся и спрашиваем.
        incident_id = self.repos.incidents.add(
            workspace_id,
            kind="conflict" if verdict.verdict == "conflict" else "contradiction",
            description=(verdict.notes
                         or "Супервайзер не принял результат после доработок"),
            severity=verdict.max_severity,
            task_id=task.id, subtask_id=subtask.id, report_id=report.id,
        )
        reason = (Reason.CONFLICT if verdict.verdict == "conflict"
                  else Reason.NOT_ACCEPTED)
        return await self._escalate(workspace_id, task, subtask, agent, report,
                                    verdict, incident_id, reason)

    async def _handle_unverified(self, workspace_id: int, task: Task, subtask: Subtask,
                                 agent: Agent, report: Report, verdict
                                 ) -> tuple[bool | None, str, bool]:
        """Супервайзер не смог проверить отчёт - решение за человеком."""
        incident_id = self.repos.incidents.add(
            workspace_id, kind="unverified",
            description=verdict.notes or "Результат не прошёл проверку супервайзера",
            severity="medium", task_id=task.id, subtask_id=subtask.id,
            report_id=report.id,
        )
        return await self._escalate(workspace_id, task, subtask, agent, report,
                                    verdict, incident_id, Reason.UNVERIFIED)

    async def _escalate(self, workspace_id: int, task: Task, subtask: Subtask,
                        agent: Agent, report: Report, verdict, incident_id: int,
                        reason: Reason) -> tuple[bool | None, str, bool]:
        """Результат не принят автоматически: спросить человека или отложить.

        Без human-in-the-loop спросить некого, поэтому результат остаётся
        на проверке и НЕ передаётся зависимым подзадачам: строить дальше на
        непринятом результате значит размножить возможную ошибку.
        """
        self.repos.tasks.update_subtask(subtask.id, status="review")
        self.state.escalated += 1
        self.repos.incidents.resolve(incident_id, "escalated",
                                     "Требуется решение пользователя")

        if self._gate is None:
            self.repos.agents.set_status(agent.id, "idle")
            self.bus.emit(Event(
                EventType.APPROVAL_REQUESTED, workspace_id=workspace_id, task_id=task.id,
                subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
                message=f"нужно решение по «{subtask.title}»: {verdict.notes[:150]}",
                payload={"incident_id": incident_id, "verdict": verdict.verdict},
            ))
            await self._maybe_summarize(task)
            return False, "", False

        self._set_status(agent, subtask, "paused")
        answer = await self._ask_human(
            reason,
            f"«{subtask.title}»: {verdict.notes[:200] or 'результат не принят'}",
            details=self._decision_details(subtask, report, verdict),
            task_id=task.id, subtask_id=subtask.id, agent_name=agent.name,
        )
        return await self._apply_decision(workspace_id, task, subtask, agent,
                                          answer, incident_id)

    def _decision_details(self, subtask: Subtask, report: Report, verdict) -> str:
        """Готовит выжимку, по которой человек может принять решение не вслепую."""
        parts = [f"Подзадача: {subtask.description[:400]}" if subtask.description else ""]
        if verdict.issues:
            parts.append("Замечания супервайзера:\n" + "\n".join(
                f"· [{i.severity}] {i.description}" for i in verdict.issues
            ))
        elif verdict.notes:
            parts.append("Супервайзер:\n" + verdict.notes[:600])
        parts.append("Результат исполнителя:\n" + (report.content or "")[:1200])
        return "\n\n".join(p for p in parts if p)

    async def _apply_decision(self, workspace_id: int, task: Task, subtask: Subtask,
                              agent: Agent, answer: Answer, incident_id: int | None
                              ) -> tuple[bool | None, str, bool]:
        """Применяет решение пользователя к подзадаче.

        Третий элемент кортежа - признак того, что круг доработки назначил
        человек, а значит его надо выдать сверх автоматического лимита.
        """
        if incident_id is not None:
            self.state.escalated = max(0, self.state.escalated - 1)

        if answer.decision is Decision.ABORT:
            self.bus.log("Прогон остановлен решением пользователя",
                         workspace_id=workspace_id, task_id=task.id)
            self._stop.set()
            self.repos.tasks.update_subtask(subtask.id, status="paused")
            self.repos.agents.set_status(agent.id, "paused")
            return False, "", False

        if answer.decision is Decision.REWORK:
            # Комментарий человека важнее замечаний супервайзера: он идёт
            # исполнителю первым и получает дополнительный круг доработки.
            self.state.reworks += 1
            self.repos.tasks.update_subtask(
                subtask.id, status="rework", rework_count=subtask.rework_count + 1
            )
            if incident_id:
                self.repos.incidents.resolve(
                    incident_id, "resolved",
                    f"Пользователь отправил на доработку: {answer.comment[:200]}"
                )
            return None, answer.comment or "Пользователь вернул работу на доработку.", True

        if answer.decision is Decision.SKIP:
            self.repos.tasks.update_subtask(subtask.id, status="error")
            self.repos.agents.set_status(agent.id, "idle")
            self.state.failed += 1
            if incident_id:
                self.repos.incidents.resolve(
                    incident_id, "resolved",
                    f"Подзадача пропущена пользователем: {answer.comment[:200]}"
                )
            self.bus.log(f"подзадача «{subtask.title}» пропущена",
                         workspace_id=workspace_id, subtask_id=subtask.id,
                         agent_name=agent.name)
            await self._maybe_summarize(task)
            return False, "", False

        # APPROVE: принимаем результат как есть
        self.repos.tasks.update_subtask(subtask.id, status="done")
        self.repos.agents.set_status(agent.id, "idle")
        self.state.finished += 1
        if incident_id:
            self.repos.incidents.resolve(
                incident_id, "resolved",
                f"Принято пользователем: {answer.comment[:200] or 'без комментария'}"
            )
        await self._maybe_summarize(task)
        return True, "", False

    async def _on_budget_blocked(self, state: ScopeState) -> bool:
        """Лимит исчерпан посреди прогона: спросить, поднимать ли его.

        Возвращает ``True``, если пользователь разрешил продолжить (лимит
        поднимает сам ``BudgetGuard``). Остановка прогона - отдельное
        решение: тогда заблокированные вызовы завершаются ошибкой.
        """
        gate = self._gate
        if gate is None or self._stop.is_set():
            return False
        answer = await gate.ask(
            Reason.BUDGET,
            f"Исчерпан лимит: {state.reason()}. Поднять лимит на 50% и продолжить?",
            details=(f"Уровень: {state.name}\n"
                     f"Текущий лимит: {BudgetGuard.describe_limit(state)}\n"
                     f"Израсходовано: {state.tokens} токенов, ~${state.cost:.4f}"),
            task_id=self.state.task_id, agent_name="Бюджет",
        )
        if answer.decision is Decision.ABORT:
            self.bus.log("Прогон остановлен: лимит бюджета исчерпан",
                         workspace_id=self._workspace_id, task_id=self.state.task_id)
            self._stop.set()
            self._pause.set()
        return answer.decision is Decision.EXTEND

    def _last_report(self, subtask_id: int) -> Report | None:
        row = self.repos.db.query_one(
            "SELECT * FROM reports WHERE subtask_id = ? ORDER BY id DESC LIMIT 1",
            (subtask_id,),
        )
        return Report.from_row(row) if row else None

    async def _maybe_summarize(self, task: Task) -> None:
        """Сводка по событию «агент завершил подзадачу», если она включена."""
        if self._supervisor is None or not self._summary_on_event:
            return
        try:
            await self._supervisor.make_summary(task, trigger="event")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Сбой сводки по событию")

    def _finish_subtask(self, workspace_id: int, task: Task, subtask: Subtask,
                        agent: Agent, result: RunResult) -> bool:
        """Сохраняет результат, создаёт отчёт для супервайзера, обновляет счётчики."""
        self.state.tokens += result.tokens_in + result.tokens_out
        self.state.cost += result.cost_usd

        self.repos.tasks.update_subtask(
            subtask.id,
            status="review" if result.ok else "error",
            result=result.result_text,
            tokens_in=subtask.tokens_in + result.tokens_in,
            tokens_out=subtask.tokens_out + result.tokens_out,
            cost_usd=subtask.cost_usd + result.cost_usd,
        )

        if not result.ok:
            self.state.failed += 1
            self.repos.agents.set_status(agent.id, "error")
            self.bus.emit(Event(
                EventType.SUBTASK_FAILED, workspace_id=workspace_id, task_id=task.id,
                subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
                message=result.error or "Подзадача не выполнена",
            ))
            return False

        # Отчёт - это то, что увидит супервайзер.
        report_id = self.repos.reports.add_report(
            workspace_id, task.id, subtask.id, agent.id,
            content=result.result_text, confidence=result.confidence,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
        )
        self.repos.agents.set_status(agent.id, "idle")

        confidence = (f", уверенность {result.confidence:.2f}"
                      if result.confidence is not None else "")
        self.bus.emit(Event(
            EventType.REPORT_CREATED, workspace_id=workspace_id, task_id=task.id,
            subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
            message=f"Отчёт по «{subtask.title}» ({result.steps} шагов{confidence})",
            payload={"report_id": report_id, "confidence": result.confidence},
        ))
        self.bus.emit(Event(
            EventType.SUBTASK_FINISHED, workspace_id=workspace_id, task_id=task.id,
            subtask_id=subtask.id, agent_id=agent.id, agent_name=agent.name,
            message=subtask.title,
        ))
        return True

    def _rework_notes(self, subtask: Subtask) -> str:
        """Замечания супервайзера к прошлой версии подзадачи."""
        if subtask.rework_count <= 0:
            return ""
        row = self.repos.db.query_one(
            "SELECT review_notes FROM reports WHERE subtask_id = ? AND review_notes <> '' "
            "ORDER BY id DESC LIMIT 1",
            (subtask.id,),
        )
        return row["review_notes"] if row else ""

    # служебное
    def _provider_for(self, agent: Agent) -> LLMProvider | None:
        """Кэширует по одному HTTP-клиенту на агента за прогон."""
        if agent.id in self._providers:
            return self._providers[agent.id]
        if not agent.model or not agent.api_key_id:
            return None
        key = self.repos.keys.get(agent.api_key_id)
        if key is None:
            return None
        secret = self.repos.keys.reveal(agent.api_key_id)
        provider = build_provider(agent.provider, secret, key.base_url)
        self._providers[agent.id] = provider
        return provider

    def _set_status(self, agent: Agent, subtask: Subtask, status: str) -> None:
        self.repos.agents.set_status(agent.id, status)
        self.repos.tasks.update_subtask(subtask.id, status=status)
        self.bus.emit(Event(EventType.AGENT_STATUS, workspace_id=self._workspace_id,
                            task_id=self.state.task_id, agent_id=agent.id,
                            agent_name=agent.name, subtask_id=subtask.id,
                            message=status, payload={"status": status}))

    def _fail(self, subtask: Subtask, agent: Agent | None, message: str) -> None:
        self.state.failed += 1
        self.repos.tasks.update_subtask(subtask.id, status="error")
        if agent:
            self.repos.agents.set_status(agent.id, "error")
        self.bus.emit(Event(
            EventType.SUBTASK_FAILED, workspace_id=self._workspace_id,
            task_id=self.state.task_id, subtask_id=subtask.id,
            agent_id=agent.id if agent else None,
            agent_name=agent.name if agent else "", message=message,
        ))

    def _start_gate(self, workspace_id: int, settings: dict) -> None:
        """Включает human-in-the-loop, если он разрешён в настройках проекта."""
        if not settings.get("human_in_the_loop", True):
            self._gate = None
            self._confidence_threshold = 0.0
            self.bus.log("Human-in-the-loop выключен - система не будет останавливаться",
                         workspace_id=workspace_id)
            return
        self._gate = ApprovalGate(self.repos, self.bus, workspace_id)
        try:
            self._confidence_threshold = float(
                settings.get("hitl_confidence_threshold", 0.5) or 0.0
            )
        except (TypeError, ValueError):
            self._confidence_threshold = 0.5

    @property
    def gate(self) -> ApprovalGate | None:
        """Ворота согласования - интерфейс отдаёт через них решения пользователя."""
        return self._gate

    @property
    def budget(self) -> BudgetGuard | None:
        """Бюджет текущего прогона - для живых индикаторов в интерфейсе."""
        return self._budget

    def _count_supervisor_usage(self, tokens: int, cost: float) -> None:
        self.state.tokens += tokens
        self.state.cost += cost

    def _start_supervisor(self, workspace_id: int, settings: dict, task: Task) -> None:
        """Поднимает супервайзера, если он настроен, и включает сводки по таймеру."""
        self._summary_on_event = bool(settings.get("summary_on_event", True))
        supervisor = Supervisor(self.repos, self.bus, workspace_id, settings,
                                budget=self._budget,
                                on_usage=self._count_supervisor_usage)
        if not supervisor.available():
            self._supervisor = None
            self.bus.log("Супервайзер не настроен - отчёты принимаются без проверки",
                         workspace_id=workspace_id, task_id=task.id)
            return
        self._supervisor = supervisor
        supervisor.start_timer(task)

    async def _cleanup(self) -> None:
        if self._gate is not None:
            self._gate.cancel_all()
            self._gate = None
        # Прогон окончен: агенты, остановленные посреди работы или
        # ожидавшие решения, больше не "работают" и не "на паузе".
        # Статус подзадачи (paused) сохраняется - по нему видно, что
        # её можно продолжить следующим запуском.
        if self._workspace_id is not None:
            for agent in self.repos.agents.list(self._workspace_id):
                if agent.status in ("running", "paused"):
                    self.repos.agents.set_status(agent.id, "idle")
        if self._budget is not None:
            self._budget.on_blocked = None
        if self._supervisor is not None:
            await self._supervisor.aclose()
            self._supervisor = None
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._providers.clear()
        self._tasks.clear()
