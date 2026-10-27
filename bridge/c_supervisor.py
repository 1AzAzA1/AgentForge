"""Супервайзер: сводки, инциденты и история решений человека."""

from __future__ import annotations

from app.config import DEFAULT_WORKSPACE_SETTINGS
from app.i18n import tr
from bridge.state import Controller, as_int, error_text, rpc, when
from core.events import EventType
from core.hitl import parse_payload
from utils.asyncutils import run_async

SEVERITY_TONES = {"low": "muted", "medium": "warning", "high": "error"}
DECISION_TONES = {"approve": "success", "rework": "warning", "skip": "muted",
                  "abort": "error", "extend": "accent", "cancelled": "muted", "": "accent"}


class SupervisorController(Controller):
    defaults = {"configured": False, "modelTitle": "", "modelShort": "", "mode": "api",
                "summarizing": False, "openIncidents": 0}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._summaries = self.list_model("summaries")
        self._incidents = self.list_model("incidents")
        self._decisions = self.list_model("decisions")

    @property
    def model_short(self) -> str:
        return self._s.get("modelShort", "")

    def _settings(self) -> dict:
        ws = self.repos.workspaces.get(self.ws_id) if self.ws_id else None
        return {**DEFAULT_WORKSPACE_SETTINGS, **(ws.settings if ws else {})}

    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            for m in (self._summaries, self._incidents, self._decisions):
                m.clear()
            self._set(configured=False, modelTitle="", modelShort="", openIncidents=0)
            return
        self._describe_model()
        triggers = {"timer": tr("sup.by_timer"), "event": tr("sup.by_event"),
                    "manual": tr("sup.by_hand"), "final": tr("sup.by_final")}
        self._summaries.set_items([
            {"id": s.id, "when": when(s.created_at), "trigger": s.trigger,
             "triggerTitle": triggers.get(s.trigger, s.trigger),
             "recipients": len([x for x in s.delivered_to.split(",") if x]),
             "content": s.content}
            for s in self.repos.reports.list_summaries(self.ws_id, limit=40)])

        incidents = self.repos.incidents.list(self.ws_id, limit=200)
        self._incidents.set_items([
            {"id": i.id, "kind": i.kind, "kindTitle": tr(f"kind.{i.kind}"),
             "severity": i.severity, "severityTitle": tr(f"sev.{i.severity}"),
             "tone": SEVERITY_TONES.get(i.severity, "warning"), "status": i.status,
             "statusTitle": tr(f"inc.{i.status}"), "open": i.status in ("open", "escalated"),
             "description": i.description, "resolution": i.resolution,
             "when": when(i.created_at)}
            for i in incidents])
        self._set(openIncidents=sum(1 for i in incidents if i.status in ("open", "escalated")))

        rows = self.repos.approvals.history(self.ws_id, limit=80)
        items = []
        for row in rows:
            payload = parse_payload(row.get("payload_json", "{}"))
            decision = row.get("decision", "")
            items.append({
                "id": row["id"], "reason": row["reason"],
                "reasonTitle": tr(f"reason.{row['reason']}"),
                "decision": decision,
                "decisionTitle": tr(f"hist.{decision or 'pending'}"),
                "tone": DECISION_TONES.get(decision, "muted"),
                "question": payload.get("question", ""),
                "agent": payload.get("agent_name", ""), "comment": row.get("comment", ""),
                "when": when(row.get("created_at")),
            })
        self._decisions.set_items(items)

    def _describe_model(self) -> None:
        s = self._settings()
        if s.get("supervisor_mode") == "local":
            model = s.get("supervisor_local_model", "")
            self._set(configured=bool(model), mode="local", modelShort=model,
                      modelTitle=tr("sup.model_local", model=model,
                                    url=s.get("supervisor_local_base_url", "")))
            return
        agent = None
        if s.get("supervisor_agent_id"):
            agent = self.repos.agents.get(as_int(s["supervisor_agent_id"]))
        if agent is None:
            agent = next((a for a in self.repos.agents.list(self.ws_id) if a.is_supervisor), None)
        if agent is None:
            self._set(configured=False, mode="api", modelShort="",
                      modelTitle=tr("sup.not_configured"))
            return
        self._set(configured=True, mode="api", modelShort=agent.model,
                  modelTitle=tr("sup.model_api", name=agent.name, model=agent.model))

    def on_event(self, event) -> None:
        if event.type in (EventType.SUMMARY_CREATED, EventType.INCIDENT_CREATED,
                          EventType.REPORT_REVIEWED, EventType.RUN_FINISHED,
                          EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED):
            self.refresh()

    @rpc
    def makeSummary(self) -> None:  # noqa: N802
        """Сводка вручную: удобно освежить контекст агентов вне прогона."""
        if self.ws_id is None or self._s.get("summarizing"):
            return
        task = self.repos.tasks.current(self.ws_id)
        if task is None:
            self.toast("warning", tr("task.no_task"), "")
            return
        from core.budget import BudgetGuard
        from core.supervisor.supervisor import Supervisor

        bus = self.backend.bus
        # Ручная сводка тоже тратит деньги и подчиняется тем же лимитам.
        budget = BudgetGuard(self.repos, bus, self.ws_id, task.id, task.token_limit)
        blocked = budget.blocking_scope(None)
        if blocked is not None:
            self.toast("warning", tr("toast.summary_failed"), blocked.reason())
            return
        supervisor = Supervisor(self.repos, bus, self.ws_id, self._settings(), budget=budget)
        if not supervisor.available():
            self.toast("warning", tr("sup.not_configured"), "")
            return
        self._set(summarizing=True)

        async def job() -> str:
            try:
                return await supervisor.make_summary(task, trigger="manual")
            finally:
                await supervisor.aclose()

        def done(content: str) -> None:
            self._set(summarizing=False)
            if content:
                self.toast("success", tr("toast.summary_done"), "")
            elif supervisor.last_error:
                # Ошибка - это не "нечего пересказывать".
                self.toast("error", tr("toast.summary_failed"), supervisor.last_error)
            else:
                self.toast("info", tr("sup.nothing_to_summarize"), "")
            if self.ready:
                self.refresh()

        def failed(exc: Exception) -> None:
            self._set(summarizing=False)
            self.toast("error", tr("toast.summary_failed"), error_text(exc))

        run_async(job(), done, failed)

    @rpc
    def resolveIncident(self, incident_id: int, text: str) -> None:  # noqa: N802
        self.repos.incidents.resolve(incident_id, "resolved",
                                     (text or "").strip() or tr("sup.closed_by_user"))
        self.refresh()
        self.backend.dashboard.schedule()
