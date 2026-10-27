"""Сборка контроллеров страниц в одном месте, чтобы Backend не знал деталей."""

from __future__ import annotations

from bridge.c_agents import AgentsController
from bridge.c_budget import BudgetController
from bridge.c_dashboard import DashboardController
from bridge.c_export import ExportController
from bridge.c_keys import KeysController
from bridge.c_prefs import PrefsController
from bridge.c_run import RunController
from bridge.c_supervisor import SupervisorController
from bridge.c_task import TaskController
from bridge.c_workspaces import WorkspacesController


def build_controllers(backend) -> dict:
    # Порядок важен: супервайзер описывает модель раньше, чем её спросит
    # карточка супервайзера на странице выполнения.
    return {
        "workspaces": WorkspacesController(backend, "workspaces"),
        "keys": KeysController(backend, "keys"),
        "agents": AgentsController(backend, "agents"),
        "task": TaskController(backend, "task"),
        "supervisor": SupervisorController(backend, "supervisor"),
        "run": RunController(backend, "run"),
        "dashboard": DashboardController(backend, "dashboard"),
        "budget": BudgetController(backend, "budget"),
        "exporter": ExportController(backend, "exporter"),
        "prefs": PrefsController(backend, "prefs"),
    }
