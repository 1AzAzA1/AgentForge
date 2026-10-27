"""Воркспейсы: параллельные проекты со своими агентами, задачей и настройками."""

from __future__ import annotations

import shutil

from app.config import DEFAULT_WORKSPACE_SETTINGS, PATHS
from app.i18n import tr
from bridge.state import Controller, fmt_money, fmt_tokens, rpc, when


class WorkspacesController(Controller):

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._model = self.list_model("model")

    @rpc
    def refresh(self) -> None:
        if not self.ready:
            return
        items = []
        for ws in self.repos.workspaces.list(self.repos.session.user_id):
            tokens, cost = self.repos.budgets.workspace_totals(ws.id)
            task = self.repos.tasks.current(ws.id)
            items.append({
                "id": ws.id, "name": ws.name, "description": ws.description,
                "agents": self.repos.workspaces.agent_count(ws.id),
                "updated": when(ws.updated_at), "active": ws.id == self.ws_id,
                "tokens": fmt_tokens(tokens), "cost": fmt_money(cost),
                "taskTitle": task.title if task else "",
            })
        self._model.set_items(items)

    @rpc
    def create(self, name: str, description: str) -> str:
        name = (name or "").strip()
        if not name:
            return tr("ws.need_name")
        ws = self.repos.workspaces.create(self.repos.session.user_id, name,
                                          (description or "").strip(),
                                          dict(DEFAULT_WORKSPACE_SETTINGS))
        PATHS.workspace_dir(ws.id).mkdir(parents=True, exist_ok=True)
        self.backend.select_workspace(ws.id)
        # Во время прогона переключение не состоится, но новый воркспейс
        # всё равно должен появиться в списке.
        self.refresh()
        self.toast("success", tr("toast.ws_created"), name)
        return ""

    @rpc
    def update(self, ws_id: int, name: str, description: str) -> str:
        name = (name or "").strip()
        if not name:
            return tr("ws.need_name")
        self.repos.workspaces.update(ws_id, name=name, description=(description or "").strip())
        self.backend.update_workspace_name()
        self.refresh()
        return ""

    @rpc
    def remove(self, ws_id: int) -> None:
        if self.backend.orchestrator and self.backend.orchestrator.state.running \
                and ws_id == self.ws_id:
            self.toast("warning", tr("toast.run_active"), tr("toast.run_active_text"))
            return
        ws = self.repos.workspaces.get(ws_id)
        self.repos.workspaces.delete(ws_id)
        shutil.rmtree(PATHS.workspace_dir(ws_id), ignore_errors=True)
        if ws_id == self.ws_id:
            remaining = self.repos.workspaces.list(self.repos.session.user_id)
            self.backend.select_workspace(remaining[0].id if remaining else None)
        else:
            self.refresh()
        self.toast("info", tr("toast.ws_deleted"), ws.name if ws else "")

    @rpc
    def select(self, ws_id: int) -> None:
        self.backend.select_workspace(ws_id)
