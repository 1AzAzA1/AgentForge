"""Планировщик: разбиение задачи моделью и граф зависимостей."""

from __future__ import annotations

import json

import core.planner as planner
from core.planner import PlannedSubtask, create_planned_subtasks, parse_plan, plan_subtasks
from providers.base import CompletionResult, LLMProvider, Usage
from tests.fakes import build_project


def test_dependencies_point_only_backwards():
    plan = parse_plan([
        {"title": "Собрать данные", "assignee_role": "researcher", "depends_on": []},
        {"title": "Посчитать", "depends_on": [1, 3, 2, "x"]},     # 3 - вперёд, 2 - сам на себя
        {"title": "Свести", "depends_on": ["1", 2, 2]},
    ])
    assert [p.depends_on for p in plan] == [[], [1], [1, 2]]
    assert plan[0].assignee_role == "researcher"


def test_numbers_follow_items_without_titles():
    plan = parse_plan([
        {"title": "A"},
        {"title": ""},                      # отбрасывается, нумерация сдвигается
        {"title": "C", "depends_on": [1]},
        {"title": "D", "depends_on": [3, 2]},
        "мусор",
    ])
    assert [p.title for p in plan] == ["A", "C", "D"]
    assert [p.depends_on for p in plan] == [[], [1], [2]]


def test_single_number_instead_of_list():
    plan = parse_plan([{"title": "A"}, {"title": "B", "depends_on": 1}])
    assert plan[1].depends_on == [1]


def test_planned_subtasks_get_agents_and_graph():
    repos, ws, task, agents, _ = build_project(subtask_count=0, agent_count=1)
    planned = [
        PlannedSubtask("Найти источники", "", "analyst"),
        PlannedSubtask("Проверить цифры", "", "analyst"),
        PlannedSubtask("Свести ответ", "", "nobody", depends_on=[1, 2]),
    ]
    ids = create_planned_subtasks(repos, ws.id, task.id, planned)
    subtasks = {s.id: s for s in repos.tasks.subtasks(task.id)}
    assert subtasks[ids[0]].agent_id == agents[0].id
    assert subtasks[ids[2]].agent_id is None
    assert subtasks[ids[2]].depends_on == f"{ids[0]},{ids[1]}"
    assert subtasks[ids[0]].depends_on in ("", None)


async def test_plan_from_model_answer(monkeypatch):
    repos, ws, task, _, _ = build_project(subtask_count=0, agent_count=2)
    answer = {"subtasks": [
        {"title": "Найти данные", "description": "...", "assignee_role": "analyst", "depends_on": []},
        {"title": "Проверить расчёт", "assignee_role": "analyst", "depends_on": []},
        {"title": "Итоговый ответ", "assignee_role": "analyst", "depends_on": [1, 2]},
    ]}

    class Model(LLMProvider):
        async def complete(self, model, messages, **kwargs):
            assert "depends_on" in messages[0].content
            return CompletionResult(text="```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```",
                                    usage=Usage(100, 50))

        async def list_models(self):
            return []

    monkeypatch.setattr(planner, "build_provider", lambda *a, **k: Model())
    plan = await plan_subtasks(repos, ws.id, task.title, task.description)
    assert [p.depends_on for p in plan] == [[], [], [1, 2]]
