"""Экспорт результата: формат с объяснением выбора, состав, путь сохранения."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import PATHS
from app.i18n import tr
from bridge.state import Controller, error_text, rpc
from core.export.bundle import ExportOptions, collect, detect_format
from core.export.exporters import ExportError, export, suggest_filename
from utils.asyncutils import run_async

FORMATS = [("markdown", "file-text"), ("docx", "file-type"), ("pdf", "book-open"),
           ("zip", "file-archive")]
OPTIONS = ["results", "reports", "summaries", "incidents", "decisions", "stats",
           "files", "anon"]
DEFAULT_OPTIONS = {"results": True, "reports": False, "summaries": False,
                   "incidents": True, "decisions": False, "stats": True,
                   "files": True, "anon": False}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class ExportController(Controller):
    defaults = {"format": "markdown", "recommended": "markdown", "reason": "", "stats": "",
                "path": "", "canExport": False, "options": dict(DEFAULT_OPTIONS),
                "lastPath": "", "lastInfo": "", "exporting": False, "nothing": True}

    def __init__(self, backend, name: str) -> None:
        super().__init__(backend, name)
        self._bundle = None

    def extras(self) -> dict:
        return {
            "formats": [{"key": k, "title": tr(f"exp.f_{k}"), "hint": tr(f"exp.fh_{k}"),
                         "icon": icon} for k, icon in FORMATS],
            "optionList": [{"key": k, "title": tr(f"exp.opt_{k}")} for k in OPTIONS],
        }

    @rpc
    def refresh(self) -> None:
        if not self.ready or self.ws_id is None:
            self._bundle = None
            self._set(canExport=False, stats="", reason="", nothing=True)
            return
        try:
            self._bundle = collect(self.repos, self.ws_id)
        except ValueError as exc:
            self._bundle = None
            self._set(canExport=False, reason=str(exc), nothing=True)
            return
        b = self._bundle
        done = sum(1 for s in b.subtasks if s.result.strip())
        nothing = done == 0 and not b.files
        fmt, reason = detect_format(b)
        self._set(stats=tr("exp.stats", results=done, total=len(b.subtasks),
                           files=len(b.files), reports=len(b.reports)),
                  recommended=fmt, reason=reason, nothing=nothing,
                  canExport=not nothing)
        self.setFormat(fmt)

    def on_event(self, event) -> None:
        from core.events import EventType

        if event.type is EventType.RUN_FINISHED:
            self.refresh()

    def reset(self) -> None:
        super().reset()
        self._bundle = None

    @rpc
    def setFormat(self, fmt: str) -> None:  # noqa: N802
        if fmt not in dict(FORMATS):
            return
        path = ""
        if self._bundle is not None:
            PATHS.ensure()
            path = str(PATHS.exports_dir / suggest_filename(self._bundle, fmt))
        self._set(format=fmt, path=path)

    @rpc
    def setOption(self, key: str, value: bool) -> None:  # noqa: N802
        if key not in OPTIONS:
            return
        options = dict(self._s.get("options", DEFAULT_OPTIONS))
        options[key] = bool(value)
        self._set(options=options)

    @rpc
    def setPath(self, path: str) -> None:  # noqa: N802
        self._set(path=(path or "").strip())

    @rpc
    def exportNow(self) -> None:  # noqa: N802
        if self._bundle is None or self._s.get("exporting") or self.ws_id is None:
            return
        raw = (self._s.get("path") or "").strip()
        if not raw:
            self.toast("warning", tr("exp.no_path"), "")
            return
        o = self._s.get("options", DEFAULT_OPTIONS)
        fmt = self._s.get("format", "markdown")
        options = ExportOptions(
            include_results=o.get("results", True), include_reports=o.get("reports", False),
            include_summaries=o.get("summaries", False),
            include_incidents=o.get("incidents", True),
            include_decisions=o.get("decisions", False),
            include_files=o.get("files", True) and fmt == "zip",
            include_stats=o.get("stats", True), anonymize=o.get("anon", False))
        repos, ws_id, target = self.repos, self.ws_id, Path(raw).expanduser()
        self._set(exporting=True)

        def job():
            # Данные собираются заново: с момента открытия экрана агенты
            # могли дописать результаты. Сборка DOCX/PDF занимает секунды,
            # поэтому в отдельном потоке.
            bundle = collect(repos, ws_id)
            return export(bundle, options, fmt, target)

        def done(result) -> None:
            self._set(exporting=False)
            info = f"{result.path.name} · {human_size(result.size)}"
            if result.note:
                info += f" · {result.note}"
            self._set(lastPath=str(result.path), lastInfo=info)
            self.toast("success", tr("toast.exported"), info)

        def failed(exc: Exception) -> None:
            self._set(exporting=False)
            text = str(exc) if isinstance(exc, ExportError) else error_text(exc)
            self.toast("error", tr("toast.export_failed"), text)

        run_async(asyncio.to_thread(job), done, failed)

    @rpc
    def openFolder(self) -> None:  # noqa: N802
        if self._s.get("lastPath"):
            self.backend.openPath(self._s["lastPath"])
