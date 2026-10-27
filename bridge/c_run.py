"""Выполнение: запуск прогона, живые рассуждения агентов, граф, лента, решения.

Самое "горячее" место интерфейса: во время прогона события идут десятками
в секунду. Поэтому фрагменты стриминга не уходят в окно на каждое событие,
а копятся и сбрасываются таймером раз в ~60 мс; ячейки графа и ленты
обновляются точечно, без пересылки списков целиком.
"""

from __future__ import annotations

from collections import defaultdict

from app.i18n import tr
from bridge.c_agents import ROLE_ICONS
from bridge.state import Controller, Timer, elide, error_text, fmt_tokens, rpc, status_title
from core.events import Event, EventType
from core.hitl import Reason
from utils.asyncutils import run_async

# как часто сбрасывать накопленный стриминг в окно
STREAM_FLUSH_MS = 60
# сколько символов рассуждения держать в карточке агента
STREAM_KEEP_CHARS = 9000
FEED_LIMIT = 600
SUPERVISOR_CARD = -1
# так подписывает свои события служба супервайзера (core/supervisor)
SUPERVISOR_NAME = "Супервайзер"

# как окрашивать события в ленте
FEED_TONES = {
    EventType.RUN_STARTED: "accent", EventType.RUN_FINISHED: "success",
    EventType.RUN_PAUSED: "warning", EventType.RUN_RESUMED: "accent",
    EventType.RUN_STOPPED: "warning", EventType.AGENT_THINKING: "muted",
    EventType.AGENT_TOOL_CALL: "tool", EventType.AGENT_TOOL_RESULT: "muted",
    EventType.SUBTASK_STARTED: "accent", EventType.SUBTASK_PROGRESS: "info",
    EventType.SUBTASK_FINISHED: "success", EventType.SUBTASK_FAILED: "error",
    EventType.REPORT_CREATED: "success", EventType.REPORT_REVIEWED: "violet",
    EventType.SUMMARY_CREATED: "violet", EventType.INCIDENT_CREATED: "error",
    EventType.APPROVAL_REQUESTED: "warning", EventType.APPROVAL_RESOLVED: "info",
    EventType.BUDGET_ALERT: "warning", EventType.BUDGET_EXCEEDED: "error",
    EventType.BUDGET_EXTENDED: "info", EventType.USAGE: "muted",
    EventType.LOG: "muted", EventType.ERROR: "error",
}
# события, которые в ленте только шумят
FEED_SKIP = {EventType.AGENT_STATUS, EventType.AGENT_DELTA, EventType.USAGE}

DECISION_TONES = {"approve": "success", "rework": "warning", "skip": "muted",
                  "abort": "error", "extend": "accent"}
REASON_ICONS = {
    Reason.CONFLICT.value: "split", Reason.NOT_ACCEPTED.value: "circle-x",
    Reason.LOW_CONFIDENCE.value: "gauge", Reason.MILESTONE.value: "flag",
    Reason.UNVERIFIED.value: "scan-eye", Reason.BUDGET.value: "wallet",
}


class _Stream:
    """Рассуждение одного агента как список отрезков [вид, текст].

    Вид отрезка (text, reasoning, tool, result, marker, error) окно
    раскрашивает само.
    """

    def __init__(self) -> None:
        self.segments: list[list[str]] = []
        self.dirty = False

    def add(self, kind: str, text: str) -> None:
        if not text:
            return
        if self.segments and self.segments[-1][0] == kind:
            self.segments[-1][1] += text
        else:
            self.segments.append([kind, text])
        size = sum(len(s[1]) for s in self.segments)
        while size > STREAM_KEEP_CHARS and len(self.segments) > 1:
            size -= len(self.segments.pop(0)[1])
        if size > STREAM_KEEP_CHARS:
            self.segments[0][1] = "…" + self.segments[0][1][-STREAM_KEEP_CHARS:]
        self.dirty = True

    def reset(self) -> None:
        self.segments.clear()
        self.dirty = True

    def view(self) -> list[list[str]]:
        return [list(s) for s in self.segments]

    def text(self) -> str:
        return "".join(t for _, t in self.segments)


class RunController(Controller):
    defaults = {"taskTitle": "", "done": 0, "total": 0, "errors": 0, "review": 0,
                "progress": 0.0, "canStart": False, "blocker": "", "levels": 0,
                "maxRows": 0, "starting": False}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._streams_model = self.list_model("streams")
        self._nodes = self.list_model("nodes")
        self._edges = self.list_model("edges")
        self._feed = self.list_model("feed")
        self._approvals = self.list_model("approvals")
        self._streams: dict[int, _Stream] = defaultdict(_Stream)
        self._feed_seq = 0
        self._max_steps = 10
        self._flush = Timer(STREAM_FLUSH_MS, self._flush_streams)

    # данные
    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            self._set(taskTitle="", done=0, total=0, errors=0, review=0, progress=0.0,
                      canStart=False, blocker=tr("ws.empty"), levels=0, maxRows=0)
            self._nodes.clear()
            self._edges.clear()
            self._streams_model.clear()
            return
        ws = self.repos.workspaces.get(self.ws_id)
        if ws is not None:
            self._max_steps = int(ws.settings.get("agent_max_steps", 10) or 10)
        task = self.repos.tasks.current(self.ws_id)
        subtasks = self.repos.tasks.subtasks(task.id) if task else []
        agents = self.repos.agents.list(self.ws_id)
        names = {a.id: a.name for a in agents}

        blocker = ""
        if task is None or not task.description.strip():
            blocker = tr("run.no_task")
        elif not subtasks:
            blocker = tr("run.no_subtasks")
        elif any(not s.agent_id for s in subtasks if s.status != "done"):
            blocker = tr("run.unassigned_short")
        elif all(s.status == "done" for s in subtasks):
            blocker = tr("run.all_done")

        done = sum(1 for s in subtasks if s.status == "done")
        self._set(taskTitle=task.title if task else "", done=done, total=len(subtasks),
                  errors=sum(1 for s in subtasks if s.status == "error"),
                  review=sum(1 for s in subtasks if s.status == "review"),
                  progress=(done / len(subtasks)) if subtasks else 0.0,
                  blocker=blocker, canStart=not blocker and not self.backend.running)
        self._fill_graph(subtasks, names)
        self._fill_streams(agents, subtasks)
        self._sync_approvals()

    def _fill_graph(self, subtasks, names: dict[int, str]) -> None:
        deps = {s.id: [int(t) for t in (s.depends_on or "").split(",") if t.strip().isdigit()]
                for s in subtasks}
        ids = set(deps)
        level: dict[int, int] = {}

        def depth(sid: int, trail: frozenset[int]) -> int:
            if sid in level:
                return level[sid]
            parents = [d for d in deps.get(sid, []) if d in ids and d not in trail]
            value = 0 if not parents else 1 + max(depth(p, trail | {sid}) for p in parents)
            level[sid] = value
            return value

        rows: dict[int, int] = defaultdict(int)
        position: dict[int, tuple[int, int]] = {}
        nodes = []
        for s in subtasks:
            lv = depth(s.id, frozenset())
            row = rows[lv]
            rows[lv] += 1
            position[s.id] = (lv, row)
            nodes.append({"id": s.id, "title": s.title,
                          "agentName": names.get(s.agent_id or -1, tr("task.unassigned")),
                          "status": s.status, "statusTitle": status_title(s.status),
                          "level": lv, "row": row, "reworks": s.rework_count,
                          "deps": len(deps[s.id])})
        status = {s.id: s.status for s in subtasks}
        edges = []
        for sid, parents in deps.items():
            for p in parents:
                if p in position and sid in position:
                    edges.append({
                        "id": f"{p}-{sid}", "source": p, "target": sid,
                        "fromLevel": position[p][0], "fromRow": position[p][1],
                        "toLevel": position[sid][0], "toRow": position[sid][1],
                        "state": ("active" if status.get(sid) in ("running", "rework")
                                  else "done" if status.get(p) == "done" else "idle"),
                    })
        self._nodes.set_items(nodes)
        self._edges.set_items(edges)
        self._set(levels=(max(level.values()) + 1) if level else 0,
                  maxRows=max(rows.values()) if rows else 0)

    def _fill_streams(self, agents, subtasks) -> None:
        current = {s.agent_id: s.title for s in subtasks
                   if s.agent_id and s.status in ("running", "rework", "paused")}
        usage = {aid: t for aid, t, _ in self.repos.budgets.usage_by_agent(self.ws_id)}
        items = []
        for a in agents:
            if a.is_supervisor:
                continue
            old = self._streams_model.find(a.id)
            prev = self._streams_model.items[old] if old >= 0 else {}
            items.append({
                "id": a.id, "name": a.name, "icon": ROLE_ICONS.get(a.role or "custom", "bot"),
                "modelName": a.model, "status": a.status, "statusTitle": status_title(a.status),
                "phase": prev.get("phase", "idle"), "subtask": current.get(a.id, prev.get("subtask", "")),
                "step": prev.get("step", 0), "maxSteps": self._max_steps,
                "segments": self._streams[a.id].view(), "lastTool": prev.get("lastTool", ""),
                "tokens": fmt_tokens(usage.get(a.id, 0)), "isSupervisor": False,
                "live": a.status == "running",
            })
        old = self._streams_model.find(SUPERVISOR_CARD)
        prev = self._streams_model.items[old] if old >= 0 else {}
        items.append({
            "id": SUPERVISOR_CARD, "name": tr("run.supervisor"), "icon": "shield-check",
            "modelName": self.backend.supervisor.model_short,
            "status": prev.get("status", "idle"),
            "statusTitle": prev.get("statusTitle", status_title("idle")),
            "phase": prev.get("phase", "idle"), "subtask": prev.get("subtask", ""),
            "step": 0, "maxSteps": 0, "segments": self._streams[SUPERVISOR_CARD].view(),
            "lastTool": "", "tokens": fmt_tokens(usage.get(None, 0)), "isSupervisor": True,
            "live": prev.get("live", False),
        })
        self._streams_model.set_items(items)

    def _sync_approvals(self) -> None:
        orch = self.backend.orchestrator
        gate = orch.gate if orch else None
        items = []
        for req in (gate.pending() if gate else []):
            items.append({
                "id": req.id, "reason": req.reason.value,
                "reasonTitle": tr(f"reason.{req.reason.value}"),
                "icon": REASON_ICONS.get(req.reason.value, "hand"),
                "question": req.question, "details": req.details,
                "agent": req.agent_name,
                "options": [{"value": d.value, "title": tr(f"decision.{d.value}"),
                             "tone": DECISION_TONES.get(d.value, "muted")} for d in req.options],
                "created": req.created_at,
            })
        self._approvals.set_items(items)

    def reset(self) -> None:
        super().reset()
        self._flush.stop()
        self._streams.clear()

    # события
    def on_event(self, event: Event) -> None:
        kind = event.type
        if kind not in FEED_SKIP:
            self._append_feed(event)

        if kind is EventType.AGENT_DELTA:
            self._stream_for(event).add(event.payload.get("stream", "text"), event.message)
            self._ensure_flush()
            return
        if kind is EventType.RUN_STARTED:
            for stream in self._streams.values():
                stream.reset()
            self._set(starting=False)
            self._ensure_flush()
        if kind is EventType.SUBTASK_STARTED and event.agent_id:
            stream = self._streams[event.agent_id]
            stream.reset()
            stream.add("marker", f"{tr('run.subtask')}: {event.message}\n")
            self._streams_model.update_row(event.agent_id, subtask=event.message,
                                           phase="thinking", step=0, live=True)
            self._ensure_flush()
        elif kind is EventType.AGENT_THINKING and event.agent_id:
            step = int(event.payload.get("step", 0) or 0)
            if step > 1:
                self._streams[event.agent_id].add("marker", f"\n\n{tr('run.step')} {step}\n")
            self._streams_model.update_row(event.agent_id, step=step, phase="thinking")
            self._ensure_flush()
        elif kind is EventType.AGENT_THINKING and event.agent_name and not event.agent_id:
            self._supervisor_line(event.message, phase="review")
        elif kind is EventType.AGENT_TOOL_CALL and event.agent_id:
            tool = event.payload.get("tool", "")
            self._streams[event.agent_id].add("tool", f"\n⚙ {elide(event.message, 220)}\n")
            self._streams_model.update_row(event.agent_id, phase="tool", lastTool=tool)
            self._ensure_flush()
        elif kind is EventType.AGENT_TOOL_RESULT and event.agent_id:
            text = event.message.split("→", 1)[-1].strip()
            self._streams[event.agent_id].add("reasoning", f"↳ {elide(text, 200)}\n")
            self._streams_model.update_row(event.agent_id, phase="thinking")
            self._ensure_flush()
        elif kind is EventType.SUBTASK_FINISHED and event.agent_id:
            self._streams_model.update_row(event.agent_id, phase="done", live=False)
        elif kind is EventType.SUBTASK_FAILED and event.agent_id:
            self._streams[event.agent_id].add("error", f"\n✕ {event.message}\n")
            self._streams_model.update_row(event.agent_id, phase="error", live=False)
            self._ensure_flush()
        elif kind is EventType.REPORT_REVIEWED:
            self._supervisor_line(event.message, phase="idle",
                                  verdict=event.payload.get("verdict", ""))
        elif kind is EventType.SUMMARY_CREATED:
            self._supervisor_line(event.message, phase="idle")
        elif kind is EventType.ERROR and not event.agent_id and event.agent_name == SUPERVISOR_NAME:
            # Проверка или сводка сорвалась: карточка не должна "проверять" вечно.
            self._supervisor_line(event.message, phase="idle", verdict="unverified")
        elif kind is EventType.AGENT_STATUS and event.agent_id:
            status = event.payload.get("status", event.message)
            self._streams_model.update_row(event.agent_id, status=status,
                                           statusTitle=status_title(status),
                                           live=status == "running")
            if event.subtask_id:
                self._nodes.update_row(event.subtask_id, status=status,
                                       statusTitle=status_title(status))
                self._update_edges(event.subtask_id, status)

        if kind in (EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED,
                    EventType.RUN_FINISHED, EventType.RUN_STOPPED):
            self._sync_approvals()
        if kind in (EventType.SUBTASK_FINISHED, EventType.SUBTASK_FAILED,
                    EventType.REPORT_REVIEWED, EventType.RUN_STARTED,
                    EventType.RUN_FINISHED, EventType.APPROVAL_RESOLVED):
            self.refresh()
        if kind is EventType.RUN_FINISHED:
            self._supervisor_line("", phase="idle", live=False)
            for row in list(self._streams_model.items):
                self._streams_model.update_row(row["id"], live=False)

    def _update_edges(self, subtask_id: int, status: str) -> None:
        for edge in self._edges.items:
            if edge["target"] == subtask_id:
                state = "active" if status in ("running", "rework") else edge["state"]
                self._edges.update_row(edge["id"], state=state)

    def _stream_for(self, event: Event) -> _Stream:
        return self._streams[event.agent_id if event.agent_id else SUPERVISOR_CARD]

    def _supervisor_line(self, message: str, phase: str, verdict: str = "",
                         live: bool | None = None) -> None:
        if message:
            kind = {"ok": "result", "rework": "tool", "conflict": "error",
                    "unverified": "error"}.get(verdict, "reasoning" if phase == "review" else "text")
            self._streams[SUPERVISOR_CARD].add(kind, f"{message}\n")
            self._ensure_flush()
        working = phase == "review"
        self._streams_model.update_row(
            SUPERVISOR_CARD, phase=phase, live=working if live is None else live,
            status="running" if working else "idle",
            statusTitle=status_title("running" if working else "idle"))

    def _ensure_flush(self) -> None:
        if not self._flush.is_active():
            self._flush.start()

    def _flush_streams(self) -> None:
        idle = True
        for agent_id, stream in self._streams.items():
            if stream.dirty:
                stream.dirty = False
                idle = False
                self._streams_model.update_row(agent_id, segments=stream.view())
        if idle and not self.backend.running:
            self._flush.stop()

    def _append_feed(self, event: Event) -> None:
        self._feed_seq += 1
        self._feed.append({
            "id": self._feed_seq, "time": event.time_short,
            "agent": event.agent_name or tr("run.system"),
            "tone": FEED_TONES.get(event.type, "muted"), "kind": event.type.value,
            "message": elide(event.message, 400),
        }, limit=FEED_LIMIT)

    # действия
    @rpc
    def start(self) -> None:
        orch = self.backend.orchestrator
        if orch is None or self.ws_id is None or orch.state.running:
            return
        self.refresh()
        if self._s.get("blocker"):
            self.toast("warning", tr("run.cannot_start"), self._s["blocker"])
            return
        task = self.repos.tasks.current(self.ws_id)
        self._set(starting=True, canStart=False)

        def done(state) -> None:
            self._set(starting=False)
            self.refresh()
            self.backend.dashboard.schedule()

        def failed(exc: Exception) -> None:
            self._set(starting=False)
            self.refresh()
            self.toast("error", tr("run.cannot_start"), error_text(exc))

        run_async(orch.run_task(self.ws_id, task.id), done, failed)

    @rpc
    def togglePause(self) -> None:  # noqa: N802
        orch = self.backend.orchestrator
        if orch is None:
            return
        if orch.state.paused:
            orch.resume()
        else:
            orch.pause()

    @rpc
    def stop(self) -> None:
        orch = self.backend.orchestrator
        if orch is not None:
            orch.stop()

    @rpc
    def decide(self, approval_id: int, decision: str, comment: str) -> bool:
        orch = self.backend.orchestrator
        gate = orch.gate if orch else None
        ok = bool(gate and gate.resolve(approval_id, decision, comment))
        self._sync_approvals()
        return ok

    @rpc
    def clearFeed(self) -> None:  # noqa: N802
        self._feed.clear()

    @rpc
    def fullText(self, agent_id: int) -> str:  # noqa: N802
        """Текст рассуждения без разметки, для копирования в буфер."""
        return self._streams[agent_id].text()
