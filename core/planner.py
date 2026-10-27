"""Автоматическое разбиение задачи на подзадачи через ИИ.

Планировщик - обычный вызов модели с требованием вернуть строгий JSON:
список подзадач с ролью исполнителя и зависимостями. Из зависимостей
получается граф: независимые подзадачи идут параллельно, а подзадача,
которой нужны чужие результаты, ждёт их. Модель берётся у
агента-супервайзера, а если он не назначен - у первого доступного агента
воркспейса.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from providers.base import ChatMessage
from providers.factory import build_provider, estimate_cost
from storage.models import Agent
from storage.repositories import Repos

log = logging.getLogger("aiorc.planner")

PLANNER_SYSTEM = """\
Ты - планировщик работ. Тебе дают формулировку задачи и список доступных \
исполнителей с их ролями. Разбей задачу на 2-6 подзадач и расставь \
зависимости между ними.

Требования к разбиению:
- каждая подзадача самодостаточна и проверяема, её результат можно предъявить;
- подзадачи не дублируют друг друга; простой задаче хватит двух;
- исполнитель подбирается по роли; если подходящего нет, ставь assignee_role = null;
- формулировки конкретные, без общих слов вроде «проработать вопрос».

Зависимости:
- depends_on - номера подзадач (с 1), результаты которых нужны этой подзадаче;
- ссылаться можно только на подзадачи с меньшим номером;
- независимые подзадачи не связывай: они пойдут параллельно;
- последняя подзадача сводит результаты и даёт итоговый ответ на всю задачу, \
поэтому зависит от всех подзадач, чьи результаты нужны для ответа.

Ответь СТРОГО одним JSON-объектом без markdown-разметки и пояснений:
{"subtasks": [{"title": "...", "description": "...", "assignee_role": "analyst", \
"depends_on": []}]}
"""


@dataclass
class PlannedSubtask:
    title: str
    description: str
    assignee_role: str | None = None
    # номера подзадач этого же плана (с 1), от которых зависит эта
    depends_on: list[int] = field(default_factory=list)


def _extract_json(text: str) -> dict:
    """Достаёт JSON, даже если модель обернула его в ```json ... ```."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def choose_planner_agent(repos: Repos, workspace_id: int) -> Agent | None:
    """Супервайзер, иначе первый включённый агент с моделью."""
    agents = repos.agents.list(workspace_id)
    for a in agents:
        if a.is_supervisor and a.model and a.api_key_id:
            return a
    for a in agents:
        if a.enabled and a.model and a.api_key_id:
            return a
    return None


async def plan_subtasks(repos: Repos, workspace_id: int,
                        task_title: str, task_text: str) -> list[PlannedSubtask]:
    """Просит модель разбить задачу; возвращает список подзадач."""
    agent = choose_planner_agent(repos, workspace_id)
    if agent is None:
        raise RuntimeError(
            "Нет ни одного агента с моделью и ключом - некому планировать. "
            "Создайте агента на вкладке «Агенты»."
        )

    roster = [
        {"name": a.name, "role": a.role}
        for a in repos.agents.list(workspace_id)
        if a.enabled and not a.is_supervisor
    ]
    user_prompt = (
        f"ЗАДАЧА: {task_title}\n\n{task_text}\n\n"
        f"ДОСТУПНЫЕ ИСПОЛНИТЕЛИ: {json.dumps(roster, ensure_ascii=False)}"
    )

    # Планирование тоже тратит бюджет, поэтому лимиты проверяются заранее.
    from core.budget import BudgetGuard
    from core.events import EventBus

    task = repos.tasks.current(workspace_id)
    guard = BudgetGuard(repos, EventBus(), workspace_id,
                        task.id if task else None, task.token_limit if task else None)
    blocked = guard.blocking_scope(agent.id)
    if blocked is not None:
        raise RuntimeError(f"Планирование не запущено: лимит исчерпан - {blocked.reason()}")

    secret = repos.keys.reveal(agent.api_key_id) if agent.api_key_id else ""
    key = repos.keys.get(agent.api_key_id) if agent.api_key_id else None
    provider = build_provider(agent.provider, secret, key.base_url if key else "")
    try:
        result = await provider.complete(
            agent.model,
            [ChatMessage("system", PLANNER_SYSTEM), ChatMessage("user", user_prompt)],
            temperature=0.3,
            max_tokens=2048,
        )
    finally:
        await provider.aclose()

    # Учёт расхода - планирование тоже стоит денег.
    cost = estimate_cost(agent.provider, agent.model,
                         result.usage.input_tokens, result.usage.output_tokens)
    repos.budgets.log_call(workspace_id, task.id if task else None, None, agent.id,
                           agent.provider,
                           agent.model, result.usage.input_tokens,
                           result.usage.output_tokens, cost)

    try:
        data = _extract_json(result.text)
    except ValueError as exc:
        log.warning("Планировщик вернул не-JSON: %s", result.text[:400])
        raise RuntimeError("Модель вернула ответ не в формате JSON. "
                           "Попробуйте ещё раз или выберите другую модель.") from exc

    # Некоторые модели отвечают голым списком вместо объекта - принимаем и так.
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("subtasks"), list):
        items = data["subtasks"]
    else:
        items = []
    out = parse_plan(items)
    if not out:
        raise RuntimeError("Модель не вернула ни одной подзадачи.")
    return out


def parse_plan(items: list) -> list[PlannedSubtask]:
    """Превращает ответ модели в план, отбрасывая ошибочные зависимости.

    Номера в ``depends_on`` относятся к исходному списку модели. Пропущенные
    (без названия) элементы сдвигают нумерацию, поэтому номера пересчитываются.
    Ссылка вперёд, на себя или на несуществующий номер выбрасывается: так в
    графе не может появиться цикл.
    """
    kept: list[tuple[int, dict]] = []
    for index, item in enumerate(items, 1):
        if isinstance(item, dict) and str(item.get("title", "")).strip():
            kept.append((index, item))
    position = {orig: new for new, (orig, _) in enumerate(kept, 1)}

    out: list[PlannedSubtask] = []
    for new_index, (_, item) in enumerate(kept, 1):
        deps: list[int] = []
        raw = item.get("depends_on") or []
        if not isinstance(raw, list):
            raw = [raw]
        for value in raw:
            try:
                dep = position.get(int(str(value).strip()))
            except ValueError:
                continue
            if dep is not None and dep < new_index and dep not in deps:
                deps.append(dep)
        out.append(PlannedSubtask(
            title=str(item.get("title", "")).strip(),
            description=str(item.get("description", "")).strip(),
            assignee_role=(str(item.get("assignee_role") or "").strip() or None),
            depends_on=deps,
        ))
    return out


def create_planned_subtasks(repos: Repos, workspace_id: int, task_id: int,
                            planned: list[PlannedSubtask]) -> list[int]:
    """Создаёт подзадачи плана, назначает исполнителей и связывает зависимости.

    Возвращает id созданных подзадач в порядке плана.
    """
    ids: list[int] = []
    for item in planned:
        agent_id = match_agent_by_role(repos, workspace_id, item.assignee_role)
        subtask = repos.tasks.add_subtask(task_id, item.title, item.description, agent_id)
        ids.append(subtask.id)
    for item, sid in zip(planned, ids):
        deps = [ids[d - 1] for d in item.depends_on if 0 < d <= len(ids)]
        if deps:
            repos.tasks.update_subtask(sid, depends_on=",".join(map(str, deps)))
    return ids


def match_agent_by_role(repos: Repos, workspace_id: int, role: str | None) -> int | None:
    """Подбирает агента под предложенную планировщиком роль."""
    wanted = str(role or "").strip().lower()
    if not wanted:
        return None
    # Модель пишет роль как вздумается: "Analyst", " analyst".
    for a in repos.agents.list(workspace_id):
        if a.enabled and not a.is_supervisor and (a.role or "").strip().lower() == wanted:
            return a.id
    return None
