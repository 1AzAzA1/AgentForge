"""Этап 7 - human-in-the-loop: реальная пауза в критических точках.

Ядро не спрашивает пользователя напрямую - оно публикует запрос в шину и
останавливается на ``asyncio.Future``. Интерфейс показывает вопрос, человек
нажимает кнопку, и ядро продолжает с его решением. Запрос и ответ пишутся
в таблицу ``approvals``, поэтому история решений сохраняется.

Важно: остановка прогона должна разблокировать все ожидания, иначе кнопка
«Стоп» не сработает, пока висит вопрос. За это отвечает ``cancel_all``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum

from core.events import Event, EventBus, EventType
from storage.db import utcnow
from storage.repositories import Repos

log = logging.getLogger("aiorc.hitl")


class Decision(str, Enum):
    """Что решил пользователь."""

    APPROVE = "approve"    # принять как есть и идти дальше
    REWORK = "rework"      # вернуть исполнителю с комментарием
    SKIP = "skip"          # пометить подзадачу как неудачную и продолжить
    ABORT = "abort"        # остановить весь прогон
    EXTEND = "extend"      # поднять исчерпанный лимит бюджета и продолжить


class Reason(str, Enum):
    """Почему система остановилась."""

    CONFLICT = "conflict"              # супервайзер нашёл противоречие
    NOT_ACCEPTED = "not_accepted"      # доработки исчерпаны, результат не принят
    LOW_CONFIDENCE = "low_confidence"  # агент сам не уверен в результате
    MILESTONE = "milestone"            # завершён этап работ
    UNVERIFIED = "unverified"          # супервайзер не смог проверить результат
    BUDGET = "budget"                  # исчерпан лимит бюджета


REASON_TITLES = {
    Reason.CONFLICT: "Конфликт данных",
    Reason.NOT_ACCEPTED: "Результат не принят супервайзером",
    Reason.LOW_CONFIDENCE: "Низкая уверенность исполнителя",
    Reason.MILESTONE: "Завершён этап работ",
    Reason.UNVERIFIED: "Результат не проверен",
    Reason.BUDGET: "Исчерпан лимит бюджета",
}

# какие кнопки показывать для каждой причины
REASON_OPTIONS: dict[Reason, list[Decision]] = {
    Reason.CONFLICT: [Decision.APPROVE, Decision.REWORK, Decision.SKIP, Decision.ABORT],
    Reason.NOT_ACCEPTED: [Decision.APPROVE, Decision.REWORK, Decision.SKIP, Decision.ABORT],
    Reason.LOW_CONFIDENCE: [Decision.APPROVE, Decision.REWORK, Decision.ABORT],
    Reason.MILESTONE: [Decision.APPROVE, Decision.ABORT],
    Reason.UNVERIFIED: [Decision.APPROVE, Decision.REWORK, Decision.SKIP, Decision.ABORT],
    Reason.BUDGET: [Decision.EXTEND, Decision.SKIP, Decision.ABORT],
}

DECISION_TITLES = {
    Decision.APPROVE: "Принять",
    Decision.REWORK: "На доработку",
    Decision.SKIP: "Пропустить",
    Decision.ABORT: "Остановить прогон",
    Decision.EXTEND: "Увеличить лимит на 50%",
}


@dataclass
class ApprovalRequest:
    """Открытый вопрос к пользователю."""

    id: int
    workspace_id: int
    task_id: int | None
    subtask_id: int | None
    reason: Reason
    question: str
    details: str = ""
    agent_name: str = ""
    options: list[Decision] = field(default_factory=list)
    created_at: str = ""


@dataclass
class Answer:
    """Ответ пользователя."""

    decision: Decision
    comment: str = ""

    @property
    def is_abort(self) -> bool:
        return self.decision is Decision.ABORT


class ApprovalGate:
    """Останавливает работу и ждёт решения человека."""

    def __init__(self, repos: Repos, bus: EventBus, workspace_id: int) -> None:
        self.repos = repos
        self.bus = bus
        self.workspace_id = workspace_id
        self._waiters: dict[int, asyncio.Future] = {}
        self._open: dict[int, ApprovalRequest] = {}
        self._aborted = False

    # запрос
    async def ask(self, reason: Reason, question: str, *, details: str = "",
                  task_id: int | None = None, subtask_id: int | None = None,
                  agent_name: str = "", options: list[Decision] | None = None,
                  default: Decision = Decision.APPROVE) -> Answer:
        """Публикует вопрос и ждёт ответа.

        Если прогон уже остановлен, вопрос не задаётся - возвращается
        ``ABORT``, чтобы вызывающий код свернул работу.
        """
        if self._aborted:
            return Answer(Decision.ABORT)

        choices = options or REASON_OPTIONS.get(reason, [Decision.APPROVE, Decision.ABORT])
        payload = {
            "subtask_id": subtask_id,
            "question": question,
            "details": details,
            "agent_name": agent_name,
            "options": [c.value for c in choices],
        }
        approval_id = self.repos.approvals.create(
            self.workspace_id, task_id, reason.value, payload
        )
        request = ApprovalRequest(
            id=approval_id, workspace_id=self.workspace_id, task_id=task_id,
            subtask_id=subtask_id, reason=reason, question=question, details=details,
            agent_name=agent_name, options=choices, created_at=utcnow(),
        )
        self._open[approval_id] = request

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters[approval_id] = future

        self.bus.emit(Event(
            EventType.APPROVAL_REQUESTED, workspace_id=self.workspace_id,
            task_id=task_id, subtask_id=subtask_id, agent_name=agent_name or "Система",
            message=f"нужно решение: {question}",
            payload={"approval_id": approval_id, "reason": reason.value,
                     "details": details,
                     "options": [c.value for c in choices]},
        ))

        try:
            answer: Answer = await future
        except asyncio.CancelledError:
            self.repos.approvals.decide(approval_id, "cancelled",
                                        "Прогон остановлен до получения решения")
            self._open.pop(approval_id, None)
            raise
        finally:
            self._waiters.pop(approval_id, None)

        self._open.pop(approval_id, None)
        self.repos.approvals.decide(approval_id, answer.decision.value, answer.comment)
        self.bus.emit(Event(
            EventType.APPROVAL_RESOLVED, workspace_id=self.workspace_id,
            task_id=task_id, subtask_id=subtask_id, agent_name="Пользователь",
            message=f"решение: {DECISION_TITLES.get(answer.decision, answer.decision.value)}"
                    + (f" - {answer.comment[:120]}" if answer.comment else ""),
            payload={"approval_id": approval_id, "decision": answer.decision.value},
        ))
        if answer.is_abort:
            self._aborted = True
        return answer

    # ответ
    def resolve(self, approval_id: int, decision: Decision | str,
                comment: str = "") -> bool:
        """Отдаёт решение ожидающему коду. Вызывается из интерфейса."""
        future = self._waiters.get(approval_id)
        if future is None or future.done():
            return False
        if isinstance(decision, str):
            try:
                decision = Decision(decision)
            except ValueError:
                # Неизвестное решение не превращаем молча в "Принять":
                # это ровно та ошибка, ради которой человека и спрашивают.
                return False
        request = self._open.get(approval_id)
        if request is not None and request.options and decision not in request.options:
            return False
        future.set_result(Answer(decision, (comment or "").strip()))
        return True

    def cancel_all(self) -> None:
        """Снимает все ожидания - нужно при остановке прогона."""
        self._aborted = True
        for approval_id, future in list(self._waiters.items()):
            if not future.done():
                future.set_result(Answer(Decision.ABORT, "Прогон остановлен"))
            self._waiters.pop(approval_id, None)
        self._open.clear()

    def pending(self) -> list[ApprovalRequest]:
        """Список открытых вопросов - интерфейс рисует их карточками."""
        return sorted(self._open.values(), key=lambda r: r.id)

    def has_pending(self) -> bool:
        return bool(self._open)


def parse_payload(raw: str) -> dict:
    """Безопасный разбор ``payload_json`` из таблицы approvals."""
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
