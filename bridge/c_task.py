"""Постановка задачи и подзадачи: вручную или разбиением через ИИ."""

from __future__ import annotations

from app.i18n import tr
from bridge.state import (
    Controller,
    as_int,
    elide,
    error_text,
    fmt_money,
    fmt_tokens,
    rpc,
    status_title,
)
from core.planner import match_agent_by_role, plan_subtasks
from utils.asyncutils import run_async

FORMATS = ["auto", "markdown", "docx", "pdf", "zip"]
FORMAT_ICONS = {"auto": "wand-sparkles", "markdown": "file-text", "docx": "file-type",
                "pdf": "book-open", "zip": "file-archive"}


def parse_limit(raw: str) -> int | None:
    raw = (raw or "").replace(" ", "").replace("_", "").strip()
    if not raw:
        return None           # пусто = без лимита
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


class TaskController(Controller):
    defaults = {"hasTask": False, "taskId": -1, "title": "", "description": "",
                "format": "auto", "tokenLimit": "", "planning": False, "agentOptions": [],
                "status": "", "statusTitle": ""}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._subtasks = self.list_model("subtasks")

    def extras(self) -> dict:
        return {"formats": [{"key": k, "title": tr(f"fmt.{k}"), "icon": FORMAT_ICONS[k]}
                            for k in FORMATS]}

    # данные
    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            self.reset()
            return
        agents = self.repos.agents.list(self.ws_id)
        options = [{"id": a.id, "name": a.name, "model": a.model} for a in agents
                   if not a.is_supervisor]
        task = self.repos.tasks.current(self.ws_id)
        if task is None:
            self._subtasks.clear()
            self._set(hasTask=False, taskId=-1, title="", description="", format="auto",
                      tokenLimit="", agentOptions=options, status="", statusTitle="")
            return
        self._set(hasTask=True, taskId=task.id, title=task.title,
                  description=task.description, format=task.result_format or "auto",
                  tokenLimit=str(task.token_limit) if task.token_limit else "",
                  agentOptions=options, status=task.status,
                  statusTitle=status_title(task.status))
        self._fill_subtasks(task.id, {a.id: a.name for a in agents})

    def _fill_subtasks(self, task_id: int, names: dict[int, str]) -> None:
        subtasks = self.repos.tasks.subtasks(task_id)
        titles = {s.id: s.title for s in subtasks}
        items = []
        for i, s in enumerate(subtasks, 1):
            deps = [int(t) for t in (s.depends_on or "").split(",") if t.strip().isdigit()]
            items.append({
                "id": s.id, "index": i, "title": s.title, "description": s.description,
                "agentId": s.agent_id or -1,
                "agentName": names.get(s.agent_id or -1, tr("task.unassigned")),
                "status": s.status, "statusTitle": status_title(s.status),
                "deps": deps, "depTitles": [titles[d] for d in deps if d in titles],
                "result": s.result, "resultPreview": elide(s.result, 220),
                "reworks": s.rework_count, "tokens": fmt_tokens(s.tokens_in + s.tokens_out),
                "cost": fmt_money(s.cost_usd),
            })
        self._subtasks.set_items(items)

    def on_event(self, event) -> None:
        from core.events import EventType

        if event.type in (EventType.AGENT_STATUS, EventType.SUBTASK_STARTED,
                          EventType.SUBTASK_FINISHED, EventType.SUBTASK_FAILED,
                          EventType.REPORT_REVIEWED, EventType.APPROVAL_RESOLVED,
                          EventType.RUN_FINISHED, EventType.RUN_STARTED):
            self.refresh()

    # задача
    @rpc
    def saveTask(self, title: str, description: str, fmt: str, limit: str) -> str:  # noqa: N802
        if self.ws_id is None:
            return tr("ws.empty")
        body = (description or "").strip()
        if not body:
            return tr("task.need_body")
        title = (title or "").strip() or tr("task.untitled")
        fmt = fmt if fmt in FORMATS else "auto"
        token_limit = parse_limit(limit)
        if (limit or "").strip() and token_limit is None:
            return tr("task.bad_limit")
        task = self.repos.tasks.current(self.ws_id)
        if task is None:
            self.repos.tasks.create(self.ws_id, title, body, fmt, token_limit)
        else:
            self.repos.tasks.update(task.id, title=title, description=body,
                                    result_format=fmt, token_limit=token_limit)
            # Лимит задачи поменяли посреди прогона: он действует сразу.
            orch = self.backend.orchestrator
            if orch is not None and orch.state.running and orch.budget is not None:
                orch.budget.reload_limits()
        self.refresh()
        self.backend.run.refresh()
        self.backend.dashboard.schedule()
        return ""

    @rpc
    def newTask(self) -> str:  # noqa: N802
        """Начать новую задачу: прежняя остаётся в истории, экспорт её видит."""
        if self.backend.running:
            return tr("toast.run_active_text")
        if self.ws_id is None:
            return tr("ws.empty")
        self.repos.tasks.create(self.ws_id, tr("task.untitled"), "", "auto", None)
        self.refresh()
        self.backend.run.refresh()
        return ""

    # подзадачи
    def _ensure_task(self) -> int | None:
        task = self.repos.tasks.current(self.ws_id) if self.ws_id else None
        return task.id if task else None

    @rpc
    def saveSubtask(self, data: dict) -> str:  # noqa: N802
        task_id = self._ensure_task()
        if task_id is None:
            return tr("task.save_first")
        sid = as_int(data.get("id"))
        if sid >= 0 and self.backend.running:
            # Правка подзадачи, которую сейчас выполняет агент, разошлась бы
            # с тем, что он делает, и с тем, что потом проверит супервайзер.
            return tr("toast.run_active_text")
        title = str(data.get("title") or "").strip()
        if not title:
            return tr("task.need_subtask_title")
        agent_id = as_int(data.get("agentId"))
        known = {s.id for s in self.repos.tasks.subtasks(task_id)}
        deps = []
        for raw in data.get("deps") or []:
            dep = as_int(raw)
            if dep in known and dep != sid and dep not in deps:
                deps.append(dep)
        if sid >= 0 and self._creates_cycle(task_id, sid, deps):
            return tr("task.dep_cycle")
        description = str(data.get("description") or "").strip()
        if sid >= 0:
            self.repos.tasks.update_subtask(
                sid, title=title, description=description,
                agent_id=agent_id if agent_id >= 0 else None,
                depends_on=",".join(str(d) for d in deps))
        else:
            st = self.repos.tasks.add_subtask(task_id, title, description,
                                              agent_id if agent_id >= 0 else None)
            if deps:
                self.repos.tasks.update_subtask(st.id, depends_on=",".join(map(str, deps)))
        self.refresh()
        self.backend.run.refresh()
        return ""

    def _creates_cycle(self, task_id: int, sid: int, deps: list[int]) -> bool:
        graph = {s.id: [int(t) for t in (s.depends_on or "").split(",") if t.strip().isdigit()]
                 for s in self.repos.tasks.subtasks(task_id)}
        graph[sid] = deps
        seen: set[int] = set()
        stack = list(deps)
        while stack:
            node = stack.pop()
            if node == sid:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, []))
        return False

    @rpc
    def removeSubtask(self, sid: int) -> None:  # noqa: N802
        if self.backend.running:
            # Удаление подзадачи посреди прогона рвёт связи в базе, и агент,
            # который её выполняет, падает на записи отчёта.
            self.toast("warning", tr("toast.run_active"), tr("toast.run_active_text"))
            return
        task_id = self._ensure_task()
        self.repos.tasks.delete_subtask(sid)
        # Ссылки на удалённую подзадачу из зависимостей других тоже убираем.
        if task_id is not None:
            for s in self.repos.tasks.subtasks(task_id):
                deps = [t for t in (s.depends_on or "").split(",") if t.strip() and t.strip() != str(sid)]
                if ",".join(deps) != (s.depends_on or ""):
                    self.repos.tasks.update_subtask(s.id, depends_on=",".join(deps))
        self.refresh()
        self.backend.run.refresh()

    @rpc
    def move(self, sid: int, delta: int) -> None:
        if self.backend.running:
            return
        task_id = self._ensure_task()
        if task_id is None:
            return
        ids = [s.id for s in self.repos.tasks.subtasks(task_id)]
        if sid not in ids:
            return
        i = ids.index(sid)
        j = i + delta
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            self.repos.tasks.reorder(ids)
            self.refresh()
            self.backend.run.refresh()

    @rpc
    def resetSubtask(self, sid: int) -> None:  # noqa: N802
        """Вернуть подзадачу в очередь: следующий прогон выполнит её заново."""
        if self.backend.running:
            self.toast("warning", tr("toast.run_active"), tr("toast.run_active_text"))
            return
        self.repos.tasks.update_subtask(sid, status="idle")
        self.refresh()
        self.backend.run.refresh()

    @rpc
    def autosplit(self) -> None:
        """ИИ разбивает задачу и сразу назначает исполнителей по ролям."""
        task = self.repos.tasks.current(self.ws_id) if self.ws_id else None
        if task is None or not task.description.strip():
            self.toast("warning", tr("task.save_first"), "")
            return
        ws_id, task_id = self.ws_id, task.id
        self._set(planning=True)

        def done(planned) -> None:
            self._set(planning=False)
            # Пока модель думала, пользователь мог выйти из профиля.
            if not self.ready:
                return
            for item in planned:
                agent_id = match_agent_by_role(self.repos, ws_id, item.assignee_role)
                self.repos.tasks.add_subtask(task_id, item.title, item.description, agent_id)
            self.refresh()
            self.backend.run.refresh()
            self.toast("success", tr("toast.planned"), tr("toast.planned_n", n=len(planned)))

        def failed(exc: Exception) -> None:
            self._set(planning=False)
            self.toast("error", tr("toast.plan_failed"), error_text(exc))

        run_async(plan_subtasks(self.repos, ws_id, task.title, task.description), done, failed)
