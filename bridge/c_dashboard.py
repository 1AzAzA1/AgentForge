"""Дашборд: метрики, прогресс, кривые расхода, агенты, лента и инциденты.

Во время прогона события идут пачками, поэтому пересчёт собирается
таймером не чаще раза в секунду; шаги рассуждений дашборд не интересуют.
"""

from __future__ import annotations

from app.i18n import tr
from bridge.c_agents import ROLE_ICONS
from bridge.state import Controller, Timer, elide, fmt_money, fmt_tokens, rpc, status_title, when
from core.events import EventType

THROTTLE_MS = 1000
SERIES_LIMIT = 300
FEED_LIMIT = 40
STATUS_ORDER = ["done", "review", "rework", "running", "paused", "error", "idle"]
QUIET = {EventType.AGENT_THINKING, EventType.AGENT_DELTA, EventType.AGENT_TOOL_CALL,
         EventType.AGENT_TOOL_RESULT}


class DashboardController(Controller):
    defaults = {"agentsCount": 0, "done": 0, "total": 0, "tokens": 0.0, "tokensText": "0",
                "cost": 0.0, "costText": "$0", "reworks": 0, "openIncidents": 0,
                "limitPct": -1.0, "taskTitle": "", "segments": [], "costSeries": [],
                "tokenSeries": [], "bars": [], "supervisorShare": 0.0}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._agents = self.list_model("agents")
        self._feed = self.list_model("feed")
        self._incidents = self.list_model("incidents")
        self._timer = Timer(THROTTLE_MS, self.refresh, single_shot=True)

    def schedule(self) -> None:
        if self.ready and not self._timer.is_active():
            self._timer.start()

    def on_event(self, event) -> None:
        if event.type not in QUIET:
            self.schedule()

    def reset(self) -> None:
        super().reset()
        self._timer.stop()

    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            self.reset()
            return
        ws_id = self.ws_id
        agents = self.repos.agents.list(ws_id)
        task = self.repos.tasks.current(ws_id)
        subtasks = self.repos.tasks.subtasks(task.id) if task else []
        tokens, cost = self.repos.budgets.workspace_totals(ws_id)
        counts = self.repos.budgets.incident_counts(ws_id)

        buckets = {k: 0 for k in STATUS_ORDER}
        for s in subtasks:
            buckets[s.status] = buckets.get(s.status, 0) + 1
        segments = [{"status": k, "title": status_title(k), "count": buckets[k]}
                    for k in STATUS_ORDER if buckets.get(k)]

        series = self.repos.budgets.usage_series(ws_id, SERIES_LIMIT)
        cost_acc = token_acc = 0.0
        cost_series, token_series = [], []
        for created, t, c in series:
            cost_acc += c
            token_acc += t
            label = when(created, "%H:%M")
            cost_series.append({"label": label, "value": round(cost_acc, 6)})
            token_series.append({"label": label, "value": token_acc})

        names = {a.id: a for a in agents}
        usage = self.repos.budgets.usage_by_agent(ws_id)
        total_tokens = sum(t for _, t, _ in usage) or 1
        bars = []
        for agent_id, t, c in usage:
            if not t:
                continue
            a = names.get(agent_id) if agent_id else None
            label = a.name if a else (tr("dash.supervisor_line") if agent_id is None
                                      else tr("dash.unknown_agent"))
            bars.append({"label": label, "value": t, "text": fmt_tokens(t),
                         "cost": fmt_money(c), "supervisor": agent_id is None})
        supervisor_tokens = sum(t for aid, t, _ in usage if aid is None)

        limit_pct = -1.0
        if task and task.token_limit:
            task_tokens, _ = self.repos.budgets.task_totals(task.id)
            limit_pct = min(task_tokens / task.token_limit, 9.99)

        self._set(agentsCount=len(agents), done=buckets.get("done", 0), total=len(subtasks),
                  tokens=float(tokens), tokensText=fmt_tokens(tokens), cost=float(cost),
                  costText=fmt_money(cost), reworks=sum(s.rework_count for s in subtasks),
                  openIncidents=counts.get("open", 0) + counts.get("escalated", 0),
                  limitPct=limit_pct, taskTitle=task.title if task else "",
                  segments=segments, costSeries=cost_series, tokenSeries=token_series,
                  bars=bars[:10], supervisorShare=supervisor_tokens / total_tokens)

        per_agent = {aid: (t, c) for aid, t, c in usage}
        doing = {s.agent_id: s.title for s in subtasks
                 if s.agent_id and s.status in ("running", "rework")}
        self._agents.set_items([
            {"id": a.id, "name": a.name, "icon": ROLE_ICONS.get(a.role or "custom", "bot"),
             "status": a.status, "statusTitle": status_title(a.status),
             "doing": doing.get(a.id, ""),
             "tokens": fmt_tokens(per_agent.get(a.id, (0, 0))[0]),
             "cost": fmt_money(per_agent.get(a.id, (0, 0.0))[1]),
             "share": per_agent.get(a.id, (0, 0))[0] / total_tokens,
             "isSupervisor": a.is_supervisor}
            for a in agents])
        self._fill_feed(names)
        self._incidents.set_items([
            {"id": i.id, "kindTitle": tr(f"kind.{i.kind}"),
             "tone": {"low": "muted", "medium": "warning", "high": "error"}.get(i.severity, "warning"),
             "statusTitle": tr(f"inc.{i.status}"), "description": elide(i.description, 160),
             "when": when(i.created_at)}
            for i in self.repos.incidents.list(ws_id, limit=FEED_LIMIT)])

    def _fill_feed(self, names: dict) -> None:
        entries = []
        verdicts = {"ok": ("dash.v_ok", "success"), "rework": ("dash.v_rework", "warning"),
                    "conflict": ("dash.v_conflict", "error"),
                    "unverified": ("dash.v_unverified", "error")}
        for r in self.repos.reports.list_reports(self.ws_id, limit=FEED_LIMIT):
            badge, tone = verdicts.get(r.review_verdict, ("", "muted"))
            agent = names.get(r.agent_id)
            entries.append({"id": f"r{r.id}", "who": agent.name if agent else tr("dash.unknown_agent"),
                            "when_raw": r.created_at, "when": when(r.created_at, "%H:%M:%S"),
                            "text": elide(r.content, 180), "tone": tone,
                            "badge": tr(badge) if badge else "",
                            "confidence": f"{r.confidence:.2f}" if r.confidence is not None else ""})
        for s in self.repos.reports.list_summaries(self.ws_id, limit=FEED_LIMIT):
            entries.append({"id": f"s{s.id}", "who": tr("dash.summary_line"),
                            "when_raw": s.created_at, "when": when(s.created_at, "%H:%M:%S"),
                            "text": elide(s.content, 180), "tone": "violet", "badge": "",
                            "confidence": ""})
        entries.sort(key=lambda e: e["when_raw"], reverse=True)
        for e in entries:
            e.pop("when_raw", None)
        self._feed.set_items(entries[:FEED_LIMIT])
