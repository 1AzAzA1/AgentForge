"""Сквозной прогон интерфейса без реальной сети: вход, данные, все экраны.

Используется двумя способами:

* ``pytest tests/test_ui.py`` - проверяет, что каждый экран открывается
  без ошибок QML и что живой прогон с фейковыми агентами доходит до конца;
* ``python tests/ui_tour.py [каталог]`` - то же самое, плюс сохраняет
  скриншоты всех экранов, чтобы их можно было посмотреть глазами.

Окно рисуется без экрана (``QT_QPA_PLATFORM=offscreen``), модели заменены
фейковым провайдером со стримингом, поэтому ни токены, ни сеть не тратятся.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if "AGENTFORGE_HOME" not in os.environ:
    os.environ["AGENTFORGE_HOME"] = tempfile.mkdtemp(prefix="aiorc_ui_")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAGES = ["dashboard", "workspaces", "agents", "task", "run", "supervisor",
         "keys", "budget", "export", "settings"]


class StreamingWorker:
    """Фабрика фейковых провайдеров, которые «печатают» ответ по кусочкам."""

    @staticmethod
    def make(name: str, confidence: str = "0.85", delay: float = 0.01):
        from providers.base import CompletionResult, LLMProvider, ToolCall, Usage

        class Provider(LLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def list_models(self):
                return ["demo-model"]

            async def aclose(self):
                return None

            async def complete(self, model, messages, *, temperature=0.7, max_tokens=2048, tools=None):
                return await self.stream_complete(model, messages, tools=tools)

            async def stream_complete(self, model, messages, *, temperature=0.7, max_tokens=2048,
                                      tools=None, on_delta=None):
                self.calls += 1
                if self.calls == 1 and tools:
                    thought = f"{name}: сначала соберу данные по теме, затем проверю цифры.\n"
                    for part in thought.split(" "):
                        if on_delta:
                            on_delta(part + " ", "reasoning")
                        await asyncio.sleep(delay)
                    return CompletionResult(
                        text="Проверю расчёт в песочнице.",
                        tool_calls=[ToolCall("c1", "code_exec", {"code": "print(21 * 2)"})],
                        usage=Usage(420, 60))
                text = (f"Итоги работы {name}. Сравнил три источника, расхождений в ключевых "
                        f"цифрах нет; оценка рынка подтверждена расчётом.\n"
                        f"CONFIDENCE: {confidence}\nRESULT:\nКраткий вывод {name}: рынок растёт "
                        f"на 18% в год, основные риски связаны с регулированием.")
                for i in range(0, len(text), 9):
                    if on_delta:
                        on_delta(text[i:i + 9], "text")
                    await asyncio.sleep(delay)
                return CompletionResult(text=text, usage=Usage(900, 180))

        return Provider()


class Tour:
    """Проходит интерфейс и складывает сообщения QML и скриншоты."""

    def __init__(self, shots_dir: Path | None = None) -> None:
        import qasync
        from PySide6.QtWidgets import QApplication

        from app.config import AppSettings
        from app.i18n import set_language

        self.shots_dir = shots_dir
        set_language("ru")
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.loop = qasync.QEventLoop(self.qapp)
        asyncio.set_event_loop(self.loop)
        from ui.app import QML_MESSAGES, UiApp

        self.messages = QML_MESSAGES
        self.ui = UiApp(AppSettings())
        self.window = self.ui.window
        self.window.setProperty("width", 1480)
        self.window.setProperty("height", 940)
        self.backend = self.ui.backend

    # утилиты
    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def play(self, coro):
        """Весь сценарий идёт внутри одного работающего цикла - как в приложении.

        Если прерывать цикл между шагами, Qt продолжает обрабатывать события
        (например, при снимке окна), и корутины агентов просыпаются вне цикла.
        """
        return self.loop.run_until_complete(coro)

    def shot(self, name: str) -> None:
        if self.shots_dir is None:
            return
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.window.grabWindow().save(str(self.shots_dir / f"{name}.png"))

    def errors(self) -> list[str]:
        # Сообщение Qt о системном каталоге шрифтов к интерфейсу не относится:
        # приложение приносит свои шрифты.
        return [m for level, m in self.messages
                if level in ("warning", "error") and "font directory" not in m]

    def shell(self):
        from PySide6.QtCore import QObject

        for obj in self.window.findChildren(QObject):
            if obj.property("pageFiles") is not None:
                return obj
        return None

    def mark(self, name: str) -> None:
        self.messages.append(("mark", name))

    def go(self, page: str) -> None:
        self.mark("go " + page)
        shell = self.shell()
        assert shell is not None, "оболочка приложения не найдена"
        shell.setProperty("page", page)
        assert shell.property("page") == page

    # сценарий
    async def login(self) -> None:
        self.shot("00_signup")
        done: list[tuple[bool, str]] = []
        self.backend.authFinished.connect(lambda ok, err: done.append((ok, err)))
        self.backend.signUp("demo", "demo-password-1", "demo-password-1")
        for _ in range(100):
            if done:
                break
            await self.wait(0.05)
        assert done and done[0][0], f"вход не удался: {done}"
        await self.wait(0.8)

    async def seed(self) -> None:
        """Проект с агентами, задачей и подзадачами - как у живого пользователя."""
        self.mark("seed")
        b = self.backend
        b.workspaces.create("Анализ рынка EdTech", "Исследование рынка онлайн-обучения для отчёта инвестору")
        b.workspaces.create("Бот поддержки", "Прототип ассистента первой линии")
        repos = b.repos
        ws_id = b.workspace_id
        # Ключ без сети: Ollama не требует секрета.
        key = repos.keys.create("Локальный Ollama", "ollama", "", "http://localhost:11434/v1")
        roles = [("Аналитик", "analyst"), ("Исследователь", "researcher"), ("Критик", "critic")]
        agents = []
        for name, role in roles:
            agents.append(repos.agents.create(
                ws_id, name, role, f"Ты - {name.lower()}.", key.id, "ollama", "qwen2.5:7b-instruct",
                {"temperature": 0.4, "max_tokens": 1024, "tools": ["web_search", "code_exec"]}))
        sup = repos.agents.create(ws_id, "Супервайзер", "supervisor", "Проверяй отчёты.", key.id,
                                  "ollama", "qwen2.5:14b-instruct", {}, is_supervisor=True)
        ws = repos.workspaces.get(ws_id)
        repos.workspaces.update(ws_id, settings={**ws.settings, "supervisor_agent_id": sup.id,
                                                 "summary_interval_minutes": 0,
                                                 "human_in_the_loop": True,
                                                 "hitl_confidence_threshold": 0.6})
        task = repos.tasks.create(ws_id, "Рынок EdTech в 2026 году",
                                  "Оценить объём рынка онлайн-обучения, ключевых игроков и риски. "
                                  "Результат: короткий отчёт для инвестора.", "docx", 60000)
        s1 = repos.tasks.add_subtask(task.id, "Собрать данные об объёме рынка", "Источники за 2024-2026", agents[1].id)
        s2 = repos.tasks.add_subtask(task.id, "Проанализировать ключевых игроков", "", agents[0].id)
        s3 = repos.tasks.add_subtask(task.id, "Оценить риски и ограничения", "", agents[2].id)
        s4 = repos.tasks.add_subtask(task.id, "Свести выводы для инвестора", "", agents[0].id)
        repos.tasks.update_subtask(s2.id, depends_on=str(s1.id))
        repos.tasks.update_subtask(s3.id, depends_on=str(s1.id))
        repos.tasks.update_subtask(s4.id, depends_on=f"{s2.id},{s3.id}")
        repos.budgets.upsert("workspace", ws_id, 500000, 5.0, 0.8)
        for c in b._controllers.values():
            c.refresh()
        await self.wait(0.3)

    async def run_with_fakes(self, decide: bool = True) -> None:
        """Живой прогон с фейковыми агентами: стриминг, граф, вопрос человеку."""
        from core.hitl import Decision
        from tests.fakes import SupervisorProvider, patch_supervisor

        orch = self.backend.orchestrator
        providers = {}
        low = {"Критик"}

        def provider_for(agent):
            if agent.id not in providers:
                providers[agent.id] = StreamingWorker.make(
                    agent.name, confidence="0.4" if agent.name in low else "0.86")
            return providers[agent.id]

        orch._provider_for = provider_for
        self.mark("run")
        patch_supervisor(SupervisorProvider())
        self.go("run")
        await self.wait(0.4)
        self.backend.run.start()
        shot_taken = False
        for _ in range(400):
            await self.wait(0.05)
            gate = orch.gate
            if gate and gate.pending():
                if not shot_taken:
                    await self.wait(0.5)
                    self.shot("05b_run_decision")
                    shot_taken = True
                if decide:
                    req = gate.pending()[0]
                    self.backend.run.decide(req.id, Decision.APPROVE.value, "Проверено вручную")
            if not orch.state.running and orch.state.task_id is not None and _ > 5:
                break
        await self.wait(0.6)


async def scenario(tour: "Tour") -> None:
    await tour.login()
    await tour.seed()
    for i, page in enumerate(PAGES):
        tour.go(page)
        await tour.wait(1.0)
        tour.shot(f"{i + 1:02d}_{page}")
    await tour.run_with_fakes()
    tour.go("run")
    await tour.wait(0.8)
    tour.shot("20_run_after")
    for page in ("dashboard", "supervisor", "task", "export"):
        tour.go(page)
        await tour.wait(1.0)
        tour.shot(f"21_{page}_after")


def main(shots_dir: str) -> int:
    tour = Tour(Path(shots_dir))
    tour.play(scenario(tour))
    tour.mark("end")
    for level, m in tour.messages:
        if level == "mark":
            print("--", m)
        elif level in ("warning", "error") and "font directory" not in m:
            print("   ", m[:160])
    errors = tour.errors()
    orch = tour.backend.orchestrator
    task = tour.backend.repos.tasks.current(tour.backend.workspace_id)
    statuses = [s.status for s in tour.backend.repos.tasks.subtasks(task.id)]
    print(f"Статусы подзадач после прогона: {statuses}")
    if statuses != ["done"] * len(statuses) or orch.state.running:
        errors.append(f"прогон не завершился успешно: {statuses}")
    print(f"Скриншоты: {shots_dir}")
    print(f"Сообщений QML с предупреждениями: {len(errors)}")
    for e in errors[:60]:
        print("  ", e)
    tour.ui.dispose()
    # Закрытие цикла останавливает поток исполнителя qasync; без этого
    # процесс падает при выходе с "QThread: Destroyed while thread is running".
    tour.loop.close()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "shots")))
