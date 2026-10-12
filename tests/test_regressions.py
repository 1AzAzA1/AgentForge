"""Регрессионные тесты: хранилище, супервайзер, оркестратор, бюджет,
провайдеры и экспорт."""

from __future__ import annotations

import asyncio

from core.budget import BudgetGuard
from core.events import EventBus, EventType
from core.export.bundle import ExportOptions, ResultBundle, detect_format
from core.export.exporters import export_docx
from core.hitl import Decision
from core.orchestrator import Orchestrator
from core.supervisor.checklist import parse_verdict
from providers.base import ChatMessage, LLMProvider, ProviderError, ToolCall, ToolSpec
from providers.gemini_provider import GeminiProvider
from providers.openai_compat import OpenAICompatProvider
from storage.models import Subtask, Task, Workspace
from tests.fakes import SupervisorProvider, Worker, build_project, drive, patch_supervisor


def _events(bus: EventBus, *types: EventType) -> list:
    seen: list = []
    bus.subscribe(lambda e: seen.append(e) if e.type in types else None)
    return seen


# хранилище


def test_change_password_keeps_search_api_key():
    repos, workspace, _, _, _ = build_project()
    ws = repos.workspaces.get(workspace.id)
    repos.workspaces.update(workspace.id, settings={
        **ws.settings, "search_api_key": repos.secrets.seal("tvly-secret")})

    assert repos.users.change_password(repos.session, "password123", "newpassword456")

    token = repos.workspaces.get(workspace.id).settings["search_api_key"]
    assert repos.secrets.open(token) == "tvly-secret"


def test_failed_statement_does_not_leave_open_transaction():
    repos, *_ = build_project()
    try:
        repos.db.execute("INSERT INTO agents(workspace_id, name, created_at) VALUES (?,?,?)",
                         (999_999, "призрак", "2026-01-01"))
    except Exception:  # noqa: BLE001 - нарушение внешнего ключа ожидаемо
        pass
    with repos.db.transaction() as conn:
        conn.execute("SELECT 1")


# супервайзер


def test_verdict_without_field_is_not_accepted():
    for raw in ("{}", '{"notes": "что-то"}', "[]", '{"verdict": "maybe"}'):
        verdict = parse_verdict(raw)
        assert verdict.verdict == "rework", raw
        assert not verdict.accepted
        assert verdict.notes


async def test_accepted_result_closes_supervisor_notes():
    repos, workspace, task, _, _ = build_project(subtask_count=1)
    patch_supervisor(SupervisorProvider(verdicts=[
        '{"verdict": "ok", "notes": "", "issues": '
        '[{"kind": "factual_error", "severity": "low", "description": "мелочь"}]}']))
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker()
    await drive(orch, workspace.id, task.id)

    statuses = {i.status for i in repos.incidents.list(workspace.id)}
    assert statuses == {"resolved"}          # не висит "ждёт решения" на дашборде


# оркестратор и исполнитель


class _Broken(LLMProvider):
    async def complete(self, model, messages, **kwargs):
        raise ProviderError("500: модель недоступна", 500)

    async def list_models(self):
        return []


async def test_failed_subtask_is_reported_once():
    repos, workspace, task, _, _ = build_project(subtask_count=1)
    patch_supervisor(SupervisorProvider())
    bus = EventBus()
    failed = _events(bus, EventType.SUBTASK_FAILED)
    orch = Orchestrator(repos, bus)
    orch._provider_for = lambda agent: _Broken()
    await drive(orch, workspace.id, task.id)

    assert len(failed) == 1
    assert "модель недоступна" in failed[0].message


async def test_exhausted_user_reworks_mark_subtask_as_error():
    repos, workspace, task, _, _ = build_project(
        subtask_count=1, settings={"human_in_the_loop": True, "max_rework_rounds": 0,
                                   "hitl_confidence_threshold": 0})
    rework = '{"verdict": "rework", "notes": "ещё раз", "issues": []}'
    patch_supervisor(SupervisorProvider(verdicts=[rework] * 10))
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker()
    state = await drive(orch, workspace.id, task.id, fallback=Decision.REWORK)

    subtask = repos.tasks.subtasks(task.id)[0]
    assert subtask.status == "error"         # не "на доработке" навсегда
    assert state.failed == 1


async def test_dependency_on_missing_subtask_does_not_block():
    repos, workspace, task, _, _ = build_project(subtask_count=1)
    subtask = repos.tasks.subtasks(task.id)[0]
    repos.tasks.update_subtask(subtask.id, depends_on="999999")
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker()
    await drive(orch, workspace.id, task.id)

    assert repos.tasks.subtasks(task.id)[0].status == "done"


async def test_agents_are_idle_after_stop():
    repos, workspace, task, agents, _ = build_project(subtask_count=2)
    patch_supervisor(SupervisorProvider())
    orch = Orchestrator(repos, EventBus())
    orch._provider_for = lambda agent: Worker(delay=0.5)
    run = asyncio.ensure_future(orch.run_task(workspace.id, task.id))
    await asyncio.sleep(0.2)
    orch.stop()
    await run

    assert {repos.agents.get(a.id).status for a in agents} == {"idle"}


# бюджет


def test_budget_limit_change_applies_to_running_guard():
    repos, workspace, task, agents, _ = build_project()
    guard = BudgetGuard(repos, EventBus(), workspace.id, task.id)
    guard.add(500, 0.0, agents[0].id)
    assert guard.blocking_scope(agents[0].id) is None

    repos.budgets.upsert("agent", agents[0].id, 400, None)
    guard.reload_limits()
    blocked = guard.blocking_scope(agents[0].id)
    assert blocked is not None and blocked.scope == "agent"


# провайдеры


def test_openai_payload_matches_api_family():
    messages = [ChatMessage("user", "привет")]
    official = OpenAICompatProvider("k")
    official.key = "openai"
    reasoning = official._payload("o4-mini", messages, 0.7, 500, None)
    assert reasoning["max_completion_tokens"] == 500
    assert "max_tokens" not in reasoning and "temperature" not in reasoning
    chat = official._payload("gpt-4o-mini", messages, 0.7, 500, None)
    assert chat["temperature"] == 0.7 and "max_tokens" not in chat

    compatible = OpenAICompatProvider("k", "https://api.groq.com/openai/v1")
    compatible.key = "groq"
    other = compatible._payload("llama-3.3-70b-versatile", messages, 0.7, 500, None)
    assert other["max_tokens"] == 500 and other["temperature"] == 0.7


def test_tool_arguments_are_always_a_dict():
    assert ToolCall.parse_args('["a", "b"]') == {"_raw": '["a", "b"]'}
    assert ToolCall.parse_args("42") == {"_raw": "42"}
    assert ToolCall.parse_args('{"path": "a.txt"}') == {"path": "a.txt"}


def test_gemini_groups_tool_responses_and_returns_signature():
    calls = [ToolCall("1", "read_file", {"path": "a"}, signature="sig-1"),
             ToolCall("2", "list_dir", {})]
    _, contents = GeminiProvider._split([
        ChatMessage("user", "задача"),
        ChatMessage("assistant", "", tool_calls=calls),
        ChatMessage("tool", "текст файла", tool_call_id="1", name="read_file"),
        ChatMessage("tool", "список", tool_call_id="2", name="list_dir"),
    ])
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert len(contents[2]["parts"]) == 2            # оба ответа одним сообщением
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig-1"


def test_gemini_schema_drops_unsupported_keys():
    spec = ToolSpec("t", "d", {"type": "object", "additionalProperties": False,
                               "properties": {"n": {"type": "integer", "default": 5}}})
    payload = GeminiProvider("k")._payload([ChatMessage("user", "x")], 0.5, 100, [spec])
    params = payload["tools"][0]["functionDeclarations"][0]["parameters"]
    assert params == {"type": "object", "properties": {"n": {"type": "integer"}}}


# экспорт


def _bundle(result: str, title: str = "T", description: str = "d") -> ResultBundle:
    ws = Workspace(1, 1, "WS", "", {}, False, "", "")
    task = Task(1, 1, title, description, "done", "auto", None, "", "")
    sub = Subtask(1, 1, None, "A", "", "done", 0, "", result, 0, 0, 0, 0.0, "", "")
    return ResultBundle(workspace=ws, task=task, subtasks=[sub])


def test_docx_export_survives_terminal_output(tmp_path):
    bundle = _bundle("вывод: \x1b[31mкрасный\x1b[0m \x00 и \x07 сигнал")
    result = export_docx(bundle, ExportOptions(), tmp_path / "r.docx")
    assert result.size > 0


def test_format_detection_ignores_word_fragments():
    # "api" внутри "capital" и "app" внутри "happy" - не про код.
    fmt, _ = detect_format(_bundle("коротко", "Столица Франции",
                                   "Назови capital и один happy fact"))
    assert fmt != "zip"
