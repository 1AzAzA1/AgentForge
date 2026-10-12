"""Тесты ядра: история агентов, локи, зависимости, бюджет, HITL и стриминг
провайдеров."""

from __future__ import annotations

import json

import httpx
import pytest

from core.budget import BudgetGuard
from core.events import EventBus, EventType
from core.hitl import ApprovalGate, Decision, Reason
from core.orchestrator import Orchestrator
from core.supervisor.checklist import Anonymizer
from providers.base import ChatMessage, ToolSpec
from storage.db import Database
from storage.repositories import Repos, UserRepo
from tests.fakes import SupervisorProvider, Worker, build_project, drive, patch_supervisor


def _events(bus: EventBus, *types: EventType) -> list:
    seen: list = []
    bus.subscribe(lambda e: seen.append(e) if not types or e.type in types else None)
    return seen


# хранилище


def test_history_returns_latest_messages_in_order():
    repos, _, task, agents, _ = build_project(subtask_count=1)
    subtask = repos.tasks.subtasks(task.id)[0]
    for i in range(10):
        repos.messages.add(agents[0].id, "user", f"сообщение {i}", subtask.id)
    rows = repos.messages.history(agents[0].id, subtask.id, limit=3)
    assert [r["content"] for r in rows] == ["сообщение 7", "сообщение 8", "сообщение 9"]


def test_change_password_is_atomic(monkeypatch):
    repos, _, _, _, key = build_project()
    original = repos.db.transaction

    def broken_transaction():
        class Boom:
            def __enter__(self_inner):
                self_inner.ctx = original()
                conn = self_inner.ctx.__enter__()

                class Proxy:
                    def executemany(self, *a):
                        return conn.executemany(*a)

                    def execute(self, sql, *a):
                        if sql.startswith("UPDATE users"):
                            raise RuntimeError("сбой диска посередине")
                        return conn.execute(sql, *a)
                return Proxy()

            def __exit__(self_inner, *exc):
                return self_inner.ctx.__exit__(*exc)
        return Boom()

    monkeypatch.setattr(repos.db, "transaction", broken_transaction)
    with pytest.raises(RuntimeError):
        repos.users.change_password(repos.session, "password123", "newpassword456")
    monkeypatch.undo()

    # Старый пароль по-прежнему открывает профиль, и ключ им же расшифровывается.
    session = repos.users.authenticate(repos.session.username, "password123")
    assert session is not None
    assert Repos(repos.db, session).keys.reveal(key.id) == "sk-secret-value"


def test_secret_codec_roundtrip_and_plain_fallback():
    repos, *_ = build_project()
    sealed = repos.secrets.seal("tvly-123")
    assert sealed.startswith("enc:") and "tvly-123" not in sealed
    assert repos.secrets.open(sealed) == "tvly-123"
    assert repos.secrets.open("legacy-plain") == "legacy-plain"
    assert repos.secrets.open("") == ""


def test_recover_interrupted_runs_resets_stale_statuses():
    repos, _, task, agents, _ = build_project(subtask_count=1)
    subtask = repos.tasks.subtasks(task.id)[0]
    repos.tasks.update(task.id, status="running")
    repos.tasks.update_subtask(subtask.id, status="running")
    repos.agents.set_status(agents[0].id, "running")
    assert repos.recover_interrupted_runs() >= 3
    assert repos.tasks.get(task.id).status == "stopped"
    assert repos.tasks.get_subtask(subtask.id).status == "paused"
    assert repos.agents.get(agents[0].id).status == "idle"


# оркестратор


async def test_agent_lock_is_taken_before_concurrency_slot():
    """Подзадачи одного агента не должны занимать слоты, ожидая свой же лок."""
    repos, ws, task, agents, _ = build_project(subtask_count=0)
    for i in range(3):
        repos.tasks.add_subtask(task.id, f"A{i}", "", agents[0].id)
    repos.tasks.add_subtask(task.id, "B", "", agents[1].id)

    bus = EventBus()
    seen = _events(bus, EventType.SUBTASK_STARTED, EventType.SUBTASK_FINISHED)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, bus)
    workers = {a.id: Worker(a.name, delay=0.05) for a in agents}
    orch._provider_for = lambda agent: workers[agent.id]

    await drive(orch, ws.id, task.id)  # параллельность по умолчанию достаточна
    repos2, ws2, task2, agents2, _ = build_project(subtask_count=0)
    for i in range(3):
        repos2.tasks.add_subtask(task2.id, f"A{i}", "", agents2[0].id)
    b = repos2.tasks.add_subtask(task2.id, "B", "", agents2[1].id)
    bus2 = EventBus()
    seen2 = _events(bus2, EventType.SUBTASK_STARTED, EventType.SUBTASK_FINISHED)
    orch2 = Orchestrator(repos2, bus2)
    workers2 = {a.id: Worker(a.name, delay=0.05) for a in agents2}
    orch2._provider_for = lambda agent: workers2[agent.id]
    await drive_with_concurrency(orch2, ws2.id, task2.id, concurrency=2)

    first_finish = next(i for i, e in enumerate(seen2) if e.type is EventType.SUBTASK_FINISHED)
    b_start = next(i for i, e in enumerate(seen2)
                   if e.type is EventType.SUBTASK_STARTED and e.subtask_id == b.id)
    assert b_start < first_finish, "агент B ждал, пока A освободит слоты"
    assert len([e for e in seen if e.type is EventType.SUBTASK_FINISHED]) == 4


async def drive_with_concurrency(orch, ws_id, task_id, concurrency):
    return await orch.run_task(ws_id, task_id, concurrency=concurrency)


async def test_failed_dependency_is_reported_as_such():
    repos, ws, task, agents, _ = build_project(subtask_count=2)
    first, second = repos.tasks.subtasks(task.id)
    repos.tasks.update_subtask(second.id, depends_on=str(first.id))
    repos.agents.update(agents[0].id, enabled=False)      # первая упадёт сразу

    bus = EventBus()
    failed = _events(bus, EventType.SUBTASK_FAILED)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, bus)
    orch._provider_for = lambda agent: Worker(agent.name)
    await drive(orch, ws.id, task.id)

    messages = " | ".join(e.message for e in failed)
    assert "не выполнена зависимость «Подзадача 1»" in messages
    assert "цикл" not in messages


async def test_cycle_is_reported_as_cycle():
    repos, ws, task, _, _ = build_project(subtask_count=2)
    first, second = repos.tasks.subtasks(task.id)
    repos.tasks.update_subtask(first.id, depends_on=str(second.id))
    repos.tasks.update_subtask(second.id, depends_on=str(first.id))
    bus = EventBus()
    failed = _events(bus, EventType.SUBTASK_FAILED)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, bus)
    orch._provider_for = lambda agent: Worker(agent.name)
    await drive(orch, ws.id, task.id)
    assert any("замкнуты в цикл" in e.message for e in failed)


async def test_unverified_result_does_not_flow_to_dependents_without_hitl():
    repos, ws, task, _, _ = build_project(subtask_count=2)
    first, second = repos.tasks.subtasks(task.id)
    repos.tasks.update_subtask(second.id, depends_on=str(first.id))
    patch_supervisor(SupervisorProvider(fail_reviews=True))
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker(agent.name)
    state = await drive(orch, ws.id, task.id)

    statuses = {s.id: s.status for s in repos.tasks.subtasks(task.id)}
    assert statuses[first.id] == "review"      # ждёт человека, не "done"
    assert statuses[second.id] == "error"      # на непроверенном не строим
    assert state.finished == 0 and state.escalated == 1
    assert any(i.kind == "unverified" for i in repos.incidents.list(ws.id))


async def test_unverified_result_asks_human_when_hitl_on():
    repos, ws, task, _, _ = build_project(
        subtask_count=1, settings={"human_in_the_loop": True,
                                   "hitl_confidence_threshold": 0.0})
    patch_supervisor(SupervisorProvider(fail_reviews=True))
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker(agent.name)
    state = await drive(orch, ws.id, task.id, answers=[(Decision.APPROVE, "проверил сам")])
    history = repos.approvals.history(ws.id)
    assert history and history[0]["reason"] == Reason.UNVERIFIED.value
    assert state.finished == 1
    assert repos.tasks.subtasks(task.id)[0].status == "done"


async def test_supervisor_spend_counts_toward_task_limit():
    repos, ws, task, _, _ = build_project(subtask_count=2, token_limit=600)
    first, second = repos.tasks.subtasks(task.id)
    repos.tasks.update_subtask(second.id, depends_on=str(first.id))
    # Исполнитель: 280 токенов; супервайзер: 400 на каждую проверку.
    patch_supervisor(SupervisorProvider(tokens=(350, 50)))
    orch = Orchestrator(repos, EventBus())
    worker = Worker("A", tokens=(200, 80))
    orch._provider_for = lambda agent: worker
    state = await drive(orch, ws.id, task.id)

    # Без учёта супервайзера лимит 600 пропустил бы обе подзадачи
    # (2 * 280 = 560). С учётом - вторая упирается в лимит.
    assert worker.calls == 1
    assert state.tokens >= 280 + 400
    assert repos.tasks.subtasks(task.id)[1].status == "error"


async def test_budget_exceeded_event_is_emitted_once():
    repos, ws, task, agents, _ = build_project(subtask_count=3, agent_count=3)
    repos.budgets.upsert("workspace", ws.id, 100, None, 0.8)
    bus = EventBus()
    exceeded = _events(bus, EventType.BUDGET_EXCEEDED)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, bus)
    orch._provider_for = lambda agent: Worker(agent.name, delay=0.02)
    await drive(orch, ws.id, task.id)
    assert len(exceeded) == 1


async def test_budget_extension_with_hitl_continues_the_run():
    repos, ws, task, agents, _ = build_project(
        subtask_count=2, agent_count=1,
        settings={"human_in_the_loop": True, "hitl_confidence_threshold": 0.0})
    first, second = repos.tasks.subtasks(task.id)
    repos.tasks.update_subtask(second.id, depends_on=str(first.id))
    repos.budgets.upsert("agent", agents[0].id, 250, None, 0.9)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, EventBus())
    worker = Worker("A", tokens=(200, 80))
    orch._provider_for = lambda agent: worker
    state = await drive(orch, ws.id, task.id, answers=[(Decision.EXTEND, "")])

    assert state.finished == 2
    reasons = [r["reason"] for r in repos.approvals.history(ws.id)]
    assert Reason.BUDGET.value in reasons
    assert repos.budgets.get("agent", agents[0].id).token_limit >= 420


async def test_steps_exhausted_on_tools_gets_a_real_summary():
    repos, ws, task, _, _ = build_project(subtask_count=1,
                                          settings={"agent_max_steps": 2})
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, EventBus())
    worker = Worker("A", always_tool=True)
    orch._provider_for = lambda agent: worker
    await drive(orch, ws.id, task.id)
    subtask = repos.tasks.subtasks(task.id)[0]
    # Два шага ушли на инструменты, третий вызов - подведение итога.
    assert worker.calls == 3
    assert subtask.result.startswith("Результат от A")
    assert "Посчитаю в песочнице" not in subtask.result


async def test_agent_output_is_streamed_to_the_bus():
    repos, ws, task, _, _ = build_project(subtask_count=1)
    bus = EventBus()
    deltas = _events(bus, EventType.AGENT_DELTA)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, bus)
    orch._provider_for = lambda agent: Worker(agent.name)
    await drive(orch, ws.id, task.id)
    text = "".join(e.message for e in deltas)
    assert "RESULT:" in text
    assert all(e.payload.get("stream") == "text" for e in deltas)


# human-in-the-loop


async def test_gate_rejects_unknown_and_foreign_decisions():
    db = Database()
    session = UserRepo(db).create("gate-user", "password123")
    repos = Repos(db, session)
    ws = repos.workspaces.create(session.user_id, "ws", "", {})
    gate = ApprovalGate(repos, EventBus(), ws.id)

    import asyncio

    asking = asyncio.ensure_future(gate.ask(Reason.MILESTONE, "Продолжать?"))
    await asyncio.sleep(0)
    request = gate.pending()[0]
    assert gate.resolve(request.id, "definitely-not-a-decision") is False
    assert gate.resolve(request.id, Decision.REWORK) is False   # не из вариантов вехи
    assert gate.resolve(request.id, Decision.APPROVE) is True
    assert (await asking).decision is Decision.APPROVE


# супервайзер


def test_scrub_replaces_whole_words_longest_first():
    anon = Anonymizer()
    names = {1: "Лев", 2: "Аналитик Пётр"}
    out = anon.scrub("Аналитик Пётр и Лев согласны; Левша тоже.", names)
    assert "Левша" in out                      # "Лев" не режет чужое слово
    assert "Пётр" not in out
    assert out.count("Исполнитель") == 2


def test_budget_guard_extend_updates_task_form_limit():
    repos, ws, task, agents, _ = build_project(subtask_count=1, token_limit=1000)
    guard = BudgetGuard(repos, EventBus(), ws.id, task.id, task.token_limit)
    state = next(s for s in guard.snapshot() if s.scope == "task")
    state.tokens = 1200
    guard.extend(state)
    assert repos.tasks.get(task.id).token_limit == 1800


# провайдеры: разбор потоков


def _sse(events: list[dict | str]) -> bytes:
    lines = []
    for ev in events:
        payload = ev if isinstance(ev, str) else json.dumps(ev, ensure_ascii=False)
        lines.append(f"data: {payload}\n\n")
    return "".join(lines).encode("utf-8")


async def test_openai_stream_assembles_text_tool_calls_and_usage():
    from providers.openai_compat import OpenAICompatProvider

    body = _sse([
        {"model": "m", "choices": [{"delta": {"reasoning_content": "думаю "}}]},
        {"choices": [{"delta": {"content": "Сейчас "}}]},
        {"choices": [{"delta": {"content": "посчитаю"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "code_exec", "arguments": "{\"co"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "de\": \"print(1)\"}"}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        "[DONE]",
    ])
    provider = OpenAICompatProvider("k", "https://example.test/v1")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    got: list[tuple[str, str]] = []
    result = await provider.stream_complete(
        "m", [ChatMessage("user", "привет")],
        tools=[ToolSpec("code_exec", "", {"type": "object"})],
        on_delta=lambda t, k: got.append((k, t)))
    await provider.aclose()

    assert result.text == "Сейчас посчитаю"
    assert result.tool_calls[0].name == "code_exec"
    assert result.tool_calls[0].arguments == {"code": "print(1)"}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)
    assert ("reasoning", "думаю ") in got


async def test_openai_stream_retries_without_stream_options():
    from providers.openai_compat import OpenAICompatProvider

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "stream_options" in payload:
            return httpx.Response(400, json={"error": {"message": "unknown field stream_options"}})
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "ок"}}]}, "[DONE]"]))

    provider = OpenAICompatProvider("", "http://localhost:1/v1")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await provider.stream_complete("m", [ChatMessage("user", "привет всем")])
    await provider.aclose()
    assert result.text == "ок"
    assert len(calls) == 2
    assert result.usage.total > 0          # расход оценён, а не ноль


async def test_anthropic_stream_assembles_blocks():
    from providers.anthropic_provider import AnthropicProvider

    body = _sse([
        {"type": "message_start", "message": {"model": "claude", "usage": {"input_tokens": 20, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Ищу "}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "tu1", "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{\"query\": \"qt"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "\"}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 15}},
        {"type": "message_stop"},
    ])
    provider = AnthropicProvider("k", "https://example.test/v1")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    result = await provider.stream_complete("claude", [ChatMessage("user", "q")])
    await provider.aclose()
    assert result.text == "Ищу "
    assert result.tool_calls[0].arguments == {"query": "qt"}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (20, 15)
    assert result.finish_reason == "tool_use"


async def test_gemini_stream_skips_thoughts_in_text():
    from providers.gemini_provider import GeminiProvider

    body = _sse([
        {"candidates": [{"content": {"parts": [{"text": "план", "thought": True}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "Ответ"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3,
                           "thoughtsTokenCount": 4}},
    ])
    provider = GeminiProvider("k", "https://example.test/v1beta")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    kinds: list[str] = []
    result = await provider.stream_complete("g", [ChatMessage("user", "q")],
                                            on_delta=lambda t, k: kinds.append(k))
    await provider.aclose()
    assert result.text == "Ответ"
    assert kinds == ["reasoning", "text"]
    assert result.usage.output_tokens == 7
