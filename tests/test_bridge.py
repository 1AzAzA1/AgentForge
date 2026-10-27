"""Мост между окном и ядром: протокол, защита соединения и живой прогон.

Окно (desktop/src) и Python договариваются только через имена: цель, метод,
ключ строки. Первые тесты сверяют эти имена с кодом окна, чтобы опечатка не
всплыла уже у пользователя.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import core.orchestrator as orchestrator_module
from app.config import AppSettings
from app.i18n import EN, RU
from bridge.backend import Backend
from bridge.pages import build_controllers
from bridge.server import origin_allowed, run_server
from bridge.state import Hub, State
from storage.db import Database
from tests.fakes import StreamingWorker, SupervisorProvider, patch_supervisor

ROOT = Path(__file__).resolve().parent.parent
WINDOW_SRC = ROOT / "desktop" / "src"
TOKEN = "test-token"


def window_sources() -> list[str]:
    return [p.read_text("utf-8") for p in WINDOW_SRC.rglob("*.tsx")]


def test_window_calls_only_existing_methods():
    """Каждый call("цель", "метод") из окна попадает в метод, открытый для окна."""
    backend = Backend(Database(), AppSettings())
    calls = set()
    for text in window_sources():
        calls |= set(re.findall(r'call(?:<[^>]+>)?\(\s*"(\w+)",\s*"(\w+)"', text))
    assert len(calls) > 40, calls
    missing = []
    for target, method in sorted(calls):
        obj = backend.target(target)
        func = getattr(obj, method, None) if obj is not None else None
        if func is None or not getattr(func, "_rpc", False):
            missing.append(f"{target}.{method}")
    assert not missing, missing


def test_window_strings_exist_in_both_languages():
    used = set()
    for text in window_sources():
        used |= set(re.findall(r'\bt\("([^"]+)"', text))
        used |= set(re.findall(r'\btr\("([^"]+)"', text))
    assert used
    assert not sorted(k for k in used if k not in RU)
    assert not sorted(k for k in used if k not in EN)


def test_every_controller_has_defaults_for_reset():
    backend = Backend(Database(), AppSettings())
    for name, controller in build_controllers(backend).items():
        snapshot = controller.snapshot()
        assert set(controller.defaults) <= set(snapshot["values"]), name


def test_state_sends_only_changes():
    hub = Hub()
    sent: list[dict] = []
    hub.add_sink(sent.extend)
    state = State(hub)
    state.name = "demo"
    rows = state.list_model("rows")

    state._set(a=1, b=2)
    state._set(a=1, b=3)
    rows.set_items([{"id": 1, "x": "a"}, {"id": 2, "x": "b"}])
    rows.update_row(2, x="b")
    rows.update_row(2, x="c")
    for i in range(3, 6):
        rows.append({"id": i, "x": "n"}, limit=3)
    hub.flush()

    assert sent[0] == {"t": "state", "target": "demo", "values": {"a": 1, "b": 2}}
    assert sent[1] == {"t": "state", "target": "demo", "values": {"b": 3}}
    assert sent[2]["op"] == "set" and len(sent[2]["items"]) == 2
    # Правка без изменений не уходит в окно.
    assert sent[3] == {"t": "list", "target": "demo", "name": "rows", "op": "update",
                       "key": 2, "changes": {"x": "c"}}
    assert [m["op"] for m in sent[4:]] == ["append"] * 3
    assert [r["id"] for r in rows.items] == [3, 4, 5]


@pytest.mark.parametrize("origin, ok", [
    (None, True), ("file://", True), ("null", True), ("http://127.0.0.1:5173", True),
    ("http://localhost:5173", True), ("https://example.com", False),
    ("http://127.0.0.1.example.com", False),
])
def test_origin_check(origin, ok):
    assert origin_allowed(origin) is ok


class Window:
    """Клиент протокола: то же, что делает окно, только без интерфейса."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.state: dict = {}
        self.events: list[tuple[str, str, list]] = []
        self.pending: dict[int, asyncio.Future] = {}
        self.seq = 0
        self.reader: asyncio.Task | None = None

    async def start(self) -> None:
        await self.ws.send(json.dumps({"t": "hello", "token": TOKEN}))
        snapshot = json.loads(await self.ws.recv())
        assert snapshot["t"] == "snapshot"
        self.state = snapshot["state"]
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            message = json.loads(raw)
            if message["t"] == "result":
                self.pending.pop(message["id"]).set_result(message)
            elif message["t"] == "batch":
                for item in message["items"]:
                    self._apply(item)

    def _apply(self, item: dict) -> None:
        target = self.state.setdefault(item["target"], {"values": {}, "lists": {}})
        if item["t"] == "state":
            target["values"].update(item["values"])
        elif item["t"] == "list":
            rows = target["lists"].setdefault(item["name"], [])
            if item["op"] == "set":
                target["lists"][item["name"]] = item["items"]
            elif item["op"] == "append":
                rows.append(item["item"])
            elif item["op"] == "update":
                for row in rows:
                    if row["id"] == item["key"]:
                        row.update(item["changes"])
        elif item["t"] == "event":
            self.events.append((item["target"], item["name"], item["args"]))

    async def call(self, target: str, method: str, *args):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        await self.ws.send(json.dumps({"t": "call", "id": self.seq, "target": target,
                                       "method": method, "args": list(args)}))
        reply = await asyncio.wait_for(future, 10)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["value"]

    def values(self, target: str) -> dict:
        return self.state[target]["values"]

    def rows(self, target: str, name: str) -> list[dict]:
        return self.state[target]["lists"].get(name, [])


@pytest.fixture
async def server():
    backend = Backend(Database(), AppSettings())
    stop = asyncio.Event()
    ports: list[int] = []
    task = asyncio.create_task(run_server(backend, TOKEN, 0, stop, ports.append))
    while not ports:
        await asyncio.sleep(0.01)
    yield backend, f"ws://127.0.0.1:{ports[0]}"
    stop.set()
    await task


async def test_connection_needs_token(server):
    _, url = server
    async with connect(url) as ws:
        await ws.send(json.dumps({"t": "hello", "token": "wrong"}))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    async with connect(url, origin="https://example.com") as ws:
        with pytest.raises(ConnectionClosed):
            await ws.recv()


async def test_hidden_methods_are_not_callable(server):
    _, url = server
    async with connect(url, max_size=None) as ws:
        window = Window(ws)
        await window.start()
        for target, method in (("backend", "change_password"), ("backend", "_open_session"),
                               ("agents", "_after_change"), ("nobody", "refresh")):
            with pytest.raises(RuntimeError, match="unknown method"):
                await window.call(target, method)


async def test_live_run_through_the_window_protocol(server, monkeypatch):
    """Вход, проект, прогон с фейковыми агентами и решение человека - как из окна."""
    backend, url = server
    monkeypatch.setattr(orchestrator_module, "Supervisor", orchestrator_module.Supervisor)
    patch_supervisor(SupervisorProvider())

    async with connect(url, max_size=None) as ws:
        window = Window(ws)
        await window.start()
        assert await window.call("backend", "signUp", "bridge", "bridge-pass-1", "bridge-pass-1") == ""
        assert window.values("backend")["loggedIn"] is True
        assert await window.call("workspaces", "create", "Проект", "") == ""
        assert await window.call("keys", "create", "ollama", "Локальный", "http://localhost:11434/v1", "") == ""

        key_id = window.rows("keys", "model")[0]["id"]
        for name in ("Аналитик", "Критик"):
            error = await window.call("agents", "save", {
                "id": -1, "name": name, "role": "analyst", "prompt": "Ты исполнитель.",
                "keyId": key_id, "model": "demo-model", "temperature": 0.3,
                "maxTokens": 512, "tools": ["code_exec"], "isSupervisor": False})
            assert error == ""
        await window.call("prefs", "setValue", "summary_interval_minutes", 0)
        await window.call("prefs", "setValue", "hitl_confidence_threshold", 0.6)
        assert await window.call("task", "saveTask", "Рынок", "Оценить рынок", "auto", "") == ""
        agents = {a["name"]: a["id"] for a in window.rows("agents", "model")}
        for title, agent in (("Собрать данные", "Аналитик"), ("Оценить риски", "Критик")):
            error = await window.call("task", "saveSubtask", {
                "id": -1, "title": title, "description": "", "agentId": agents[agent], "deps": []})
            assert error == ""
        assert window.values("run")["canStart"] is True

        fakes: dict[int, StreamingWorker] = {}
        backend.orchestrator._provider_for = lambda agent: fakes.setdefault(
            agent.id, StreamingWorker(agent.name, "0.4" if agent.name == "Критик" else "0.9", delay=0))

        await window.call("run", "start")
        decided = False
        for _ in range(500):
            await asyncio.sleep(0.02)
            approvals = window.rows("run", "approvals")
            if approvals and not decided:
                assert await window.call("run", "decide", approvals[0]["id"], "approve", "проверено")
                decided = True
            run = window.values("run")
            if decided and not window.values("backend")["running"] and run["done"] == run["total"]:
                break

        assert decided, "человека не спросили о низкой уверенности"
        assert window.values("run")["done"] == 2
        assert [n["status"] for n in window.rows("run", "nodes")] == ["done", "done"]
        streams = {s["name"]: s for s in window.rows("run", "streams")}
        kinds = {kind for kind, _ in streams["Аналитик"]["segments"]}
        assert {"marker", "reasoning", "tool", "text"} <= kinds, kinds
        assert any(f["tone"] == "success" for f in window.rows("run", "feed"))
        assert ("backend", "toast") in {(t, n) for t, n, _ in window.events}

        text = await window.call("run", "fullText", agents["Аналитик"])
        assert "RESULT" in text
        await window.call("backend", "logout")
        assert window.values("backend")["loggedIn"] is False
        assert window.rows("run", "streams") == []
