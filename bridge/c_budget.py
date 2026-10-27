"""Бюджеты: лимиты по токенам и деньгам на проект, задачу и каждого агента."""

from __future__ import annotations

from app.i18n import tr
from bridge.state import Controller, as_float, fmt_money, fmt_tokens, rpc
from core.budget import load_states
from core.events import EventType

SCOPE_ICONS = {"workspace": "layers", "task": "list-checks", "agent": "bot"}


def parse_int(raw: str) -> int | None:
    raw = (raw or "").replace(" ", "").replace("_", "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_money(raw: str) -> float | None:
    raw = (raw or "").replace(",", ".").replace("$", "").replace(" ", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


class BudgetController(Controller):
    defaults = {"alert": "", "alertTone": ""}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._model = self.list_model("model")

    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            self._model.clear()
            return
        items = []
        for st in load_states(self.repos, self.ws_id):
            items.append({
                "id": f"{st.scope}:{st.scope_id}", "scope": st.scope, "scopeId": st.scope_id,
                "scopeTitle": tr(f"scope.{st.scope}"), "icon": SCOPE_ICONS.get(st.scope, "wallet"),
                "name": st.name, "tokens": st.tokens, "cost": st.cost,
                "tokenLimit": str(st.limit.token_limit) if st.limit.token_limit else "",
                "costLimit": f"{st.limit.cost_limit:g}" if st.limit.cost_limit else "",
                "threshold": float(st.limit.alert_threshold or 0.8),
                "ratio": float(st.ratio()), "exceeded": st.exceeded(),
                "isSet": st.limit.is_set, "tokensText": fmt_tokens(st.tokens),
                "costText": fmt_money(st.cost),
            })
        self._model.set_items(items)

    def on_event(self, event) -> None:
        if event.type in (EventType.BUDGET_ALERT, EventType.BUDGET_EXCEEDED,
                          EventType.BUDGET_EXTENDED):
            tone = {EventType.BUDGET_EXCEEDED: "error",
                    EventType.BUDGET_ALERT: "warning"}.get(event.type, "info")
            self._set(alert=event.message, alertTone=tone)
            self.refresh()
        elif event.type is EventType.RUN_FINISHED:
            self.refresh()

    @rpc
    def save(self, scope: str, scope_id: int, token_limit: str, cost_limit: str,
             threshold: float) -> str:
        """Сохраняет лимит; пустые поля означают "без ограничения"."""
        tokens = parse_int(token_limit)
        cost = parse_money(cost_limit)
        if (token_limit or "").strip() and tokens is None:
            return tr("bud.bad_tokens")
        if (cost_limit or "").strip() and cost is None:
            return tr("bud.bad_cost")
        threshold = min(max(as_float(threshold, 0.8) or 0.8, 0.1), 1.0)
        if scope == "task":
            # Лимит токенов задачи живёт и в форме задачи: держим их в согласии.
            self.repos.tasks.update(scope_id, token_limit=tokens)
        if tokens is None and cost is None:
            self.repos.budgets.delete_limit(scope, scope_id)
        else:
            self.repos.budgets.upsert(scope, scope_id, tokens, cost, round(threshold, 2))
        # Идёт прогон: новый лимит действует сразу, а не со следующего запуска.
        orch = self.backend.orchestrator
        if orch is not None and orch.state.running and orch.budget is not None:
            orch.budget.reload_limits()
        self.refresh()
        self.backend.task.refresh()
        return ""

    @rpc
    def dismissAlert(self) -> None:  # noqa: N802
        self._set(alert="", alertTone="")
