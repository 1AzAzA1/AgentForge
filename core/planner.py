"""Автоматическое разбиение задачи на подзадачи через ИИ (этап 3).

Планировщик - обычный вызов модели с требованием вернуть строгий JSON.
Модель берётся у агента-супервайзера, а если он не назначен - у первого
доступного агента воркспейса.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from providers.base import ChatMessage
from providers.factory import build_provider, estimate_cost
from storage.models import Agent
from storage.repositories import Repos

log = logging.getLogger("aiorc.planner")

PLANNER_SYSTEM = """\
Ты - планировщик работ. Тебе дают формулировку задачи и список доступных \
исполнителей с их ролями. Разбей задачу на 3-8 последовательных подзадач.

Требования к разбиению:
- каждая подзадача самодостаточна и проверяема, её результат можно предъявить;
- подзадачи не дублируют друг друга;
- исполнитель подбирается по роли; если подходящего нет, ставь assignee_role = null;
- формулировки конкретные, без общих слов вроде «проработать вопрос».

Ответь СТРОГО одним JSON-объектом без markdown-разметки и пояснений:
{"subtasks": [{"title": "...", "description": "...", "assignee_role": "analyst"}]}
"""


@dataclass
class PlannedSubtask:
    title: str
    description: str
    assignee_role: str | None = None


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
    out: list[PlannedSubtask] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        out.append(PlannedSubtask(
            title=title,
            description=str(item.get("description", "")).strip(),
            assignee_role=(str(item.get("assignee_role") or "").strip() or None),
        ))
    if not out:
        raise RuntimeError("Модель не вернула ни одной подзадачи.")
    return out


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
