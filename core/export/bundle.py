"""Этап 8 - сборка результата проекта.

Формат результата зависит от задачи, поэтому экспорт устроен в два слоя:

1. Из базы и рабочего каталога собирается ``ResultBundle`` - всё, что
   наработал проект.
2. Из него строится **единая модель документа** (список блоков), и уже её
   рендерят четыре формата. Благодаря этому Markdown, DOCX и PDF получаются
   одинаковыми по содержанию: правится один сборщик, а не три экспортёра.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.config import PATHS
from core.hitl import parse_payload
from storage.db import local_time
from storage.models import Incident, Report, Subtask, Summary, Task, Workspace
from storage.repositories import Repos

log = logging.getLogger("aiorc.export")

# расширения, по которым распознаём "проект с кодом"
CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb",
    ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".scala", ".sh",
    ".sql", ".html", ".css", ".scss", ".vue", ".yml", ".yaml", ".toml",
}
# служебные каталоги, которые не попадают в экспорт
SKIP_DIRS = {".sandbox", "__pycache__", ".git", "node_modules", ".venv"}

FORMAT_TITLES = {
    "markdown": "Markdown (.md)",
    "docx": "Документ Word (.docx)",
    "pdf": "Документ PDF (.pdf)",
    "zip": "ZIP-архив с файлами",
}


# ---------------------------------------------------------------------------
# Модель документа
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """Единица содержания. ``kind`` определяет, как её рисовать."""

    kind: str                     # heading | text | code | bullets | divider | meta
    text: str = ""
    level: int = 1                # для heading
    items: list[str] = field(default_factory=list)   # для bullets
    language: str = ""            # для code


def heading(text: str, level: int = 1) -> Block:
    return Block("heading", text=text, level=level)


def text(body: str) -> Block:
    return Block("text", text=body)


def bullets(items: list[str]) -> Block:
    return Block("bullets", items=[i for i in items if i])


def code(body: str, language: str = "") -> Block:
    return Block("code", text=body, language=language)


def divider() -> Block:
    return Block("divider")


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------


@dataclass
class ExportOptions:
    """Что включать в выгрузку. Значения по умолчанию - «полезное без шума»."""

    include_results: bool = True       # результаты подзадач (суть работы)
    include_reports: bool = False      # полные отчёты агентов
    include_summaries: bool = False    # сводки супервайзера
    include_incidents: bool = True     # что пошло не так и чем кончилось
    include_decisions: bool = False    # решения human-in-the-loop
    include_files: bool = True         # файлы из рабочего каталога (для ZIP)
    include_stats: bool = True         # расход токенов и стоимость
    anonymize: bool = False            # скрыть имена агентов в документе


@dataclass
class ResultBundle:
    """Всё, что наработал проект."""

    workspace: Workspace
    task: Task | None
    subtasks: list[Subtask] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    summaries: list[Summary] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    agent_names: dict[int, str] = field(default_factory=dict)
    files: list[Path] = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0

    @property
    def workspace_dir(self) -> Path:
        return PATHS.workspace_dir(self.workspace.id)

    def agent_name(self, agent_id: int | None, anonymize: bool = False) -> str:
        if agent_id is None:
            return "Супервайзер"
        if anonymize:
            ordered = sorted(self.agent_names)
            if agent_id not in ordered:
                # Удалённый агент не должен получить чужую метку "A".
                return "Исполнитель ?"
            index = ordered.index(agent_id)
            suffix = chr(ord("A") + index) if index < 26 else str(index + 1)
            return f"Исполнитель {suffix}"
        return self.agent_names.get(agent_id, "Агент удалён")

    def has_code(self) -> bool:
        return any(f.suffix.lower() in CODE_SUFFIXES for f in self.files)


def collect(repos: Repos, workspace_id: int) -> ResultBundle:
    """Собирает результат проекта из БД и рабочего каталога."""
    workspace = repos.workspaces.get(workspace_id)
    if workspace is None:
        raise ValueError("Воркспейс не найден")

    task = repos.tasks.current(workspace_id)
    subtasks = repos.tasks.subtasks(task.id) if task else []
    # В воркспейсе может быть несколько задач подряд: в документ идёт только
    # текущая, иначе отчёты и расход прошлых задач смешались бы с новыми.
    tokens, cost = (repos.budgets.task_totals(task.id) if task
                    else repos.budgets.workspace_totals(workspace_id))

    def of_task(items):
        return [i for i in items if task is None or i.task_id in (task.id, None)]

    decisions = [row for row in repos.approvals.history(workspace_id, limit=200)
                 if task is None or row.get("task_id") in (task.id, None)]

    bundle = ResultBundle(
        workspace=workspace,
        task=task,
        subtasks=subtasks,
        reports=of_task(repos.reports.list_reports(workspace_id, limit=500)),
        summaries=of_task(repos.reports.list_summaries(workspace_id, limit=100)),
        incidents=of_task(repos.incidents.list(workspace_id, limit=500)),
        decisions=decisions,
        agent_names={a.id: a.name for a in repos.agents.list(workspace_id)},
        files=scan_files(PATHS.workspace_dir(workspace_id)),
        tokens=tokens,
        cost=cost,
    )
    return bundle


def scan_files(root: Path, limit: int = 2000) -> list[Path]:
    """Файлы рабочего каталога без служебных каталогов и скрытых файлов."""
    if not root.exists():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


# ---------------------------------------------------------------------------
# Автоопределение формата
# ---------------------------------------------------------------------------

# слова, по которым задача похожа на "сделать документ"
DOC_HINTS = ("документ", "отчёт", "отчет", "статья", "текст", "инструкция",
             "руководство", "план", "анализ", "обзор", "презентация",
             "report", "document", "article", "guide", "analysis")
# слова, по которым задача похожа на "написать код"
CODE_HINTS = ("код", "программ", "скрипт", "приложение", "сервис", "api",
              "библиотек", "рефактор", "баг", "тест", "code", "script",
              "app", "service", "library", "refactor")


def detect_format(bundle: ResultBundle) -> tuple[str, str]:
    """Возвращает ``(формат, объяснение)``.

    Объяснение показывается пользователю, чтобы автоопределение не выглядело
    магией и его можно было осознанно переопределить.
    """
    if bundle.task and bundle.task.result_format not in ("", "auto"):
        chosen = bundle.task.result_format
        return chosen, "Формат задан вручную при постановке задачи."

    if bundle.has_code():
        count = sum(1 for f in bundle.files if f.suffix.lower() in CODE_SUFFIXES)
        return "zip", (f"В рабочем каталоге найдено файлов с кодом: {count}. "
                       f"Архив сохранит структуру каталогов.")

    if len(bundle.files) > 3:
        return "zip", (f"В рабочем каталоге {len(bundle.files)} файлов - "
                       f"архив удобнее одного документа.")

    haystack = " ".join(filter(None, [
        bundle.task.title if bundle.task else "",
        bundle.task.description if bundle.task else "",
    ])).lower()

    # Совпадение с начала слова: иначе "api" находится в "capital", а "код"
    # в "эпизоде", и задача про историю уходит в ZIP как "код".
    def mentions(words: tuple[str, ...]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(w)}", haystack) for w in words)

    if mentions(CODE_HINTS):
        return "zip", "Формулировка задачи говорит о коде - собираем архив."
    if mentions(DOC_HINTS):
        return "docx", "Формулировка задачи говорит о документе."

    total = sum(len(s.result) for s in bundle.subtasks)
    if total > 20_000:
        return "docx", "Результат объёмный - документ Word удобнее читать."
    return "markdown", "Результат текстовый и компактный - подойдёт Markdown."


# ---------------------------------------------------------------------------
# Построение документа
# ---------------------------------------------------------------------------


def build_document(bundle: ResultBundle, options: ExportOptions) -> list[Block]:
    """Собирает единую модель документа, общую для всех форматов."""
    blocks: list[Block] = []
    task = bundle.task

    blocks.append(heading(task.title if task and task.title else bundle.workspace.name, 1))
    blocks.append(Block("meta", text=(
        f"Проект: {bundle.workspace.name}   ·   "
        f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )))

    if task and task.description:
        blocks.append(heading("Задача", 2))
        blocks.append(text(task.description))

    if options.include_results and bundle.subtasks:
        blocks.append(heading("Результат", 2))
        done = [s for s in bundle.subtasks if s.result.strip()]
        if not done:
            blocks.append(text("Готовых результатов пока нет: ни одна подзадача "
                               "не завершилась успешно."))
        for index, subtask in enumerate(done, 1):
            blocks.append(heading(f"{index}. {subtask.title}", 3))
            author = bundle.agent_name(subtask.agent_id, options.anonymize)
            status = _status_title(subtask.status)
            blocks.append(Block("meta", text=f"{author}   ·   {status}"))
            if subtask.description:
                blocks.append(Block("meta", text=subtask.description))
            blocks.extend(_body_blocks(subtask.result))

    if options.include_summaries and bundle.summaries:
        blocks.append(divider())
        blocks.append(heading("Сводки супервайзера", 2))
        for summary in reversed(bundle.summaries):
            blocks.append(Block("meta", text=_when(summary.created_at)))
            blocks.extend(_body_blocks(summary.content))

    if options.include_reports and bundle.reports:
        blocks.append(divider())
        blocks.append(heading("Отчёты исполнителей", 2))
        for report in reversed(bundle.reports):
            author = bundle.agent_name(report.agent_id, options.anonymize)
            confidence = (f"   ·   уверенность {report.confidence:.2f}"
                          if report.confidence is not None else "")
            blocks.append(heading(f"{author}{confidence}", 3))
            blocks.append(Block("meta", text=_when(report.created_at)))
            blocks.extend(_body_blocks(report.content))

    if options.include_incidents and bundle.incidents:
        blocks.append(divider())
        blocks.append(heading("Инциденты", 2))
        blocks.append(text(
            "Что супервайзер счёл проблемой и чем это закончилось."
        ))
        blocks.append(bullets([
            f"[{i.severity}] {i.kind}: {i.description}"
            + (f" → {i.resolution}" if i.resolution else "")
            for i in reversed(bundle.incidents)
        ]))

    if options.include_decisions and bundle.decisions:
        blocks.append(divider())
        blocks.append(heading("Решения пользователя", 2))
        rows: list[str] = []
        for row in reversed(bundle.decisions):
            payload = parse_payload(row.get("payload_json", "{}"))
            decision = row.get("decision") or "ожидает решения"
            comment = f" - {row['comment']}" if row.get("comment") else ""
            rows.append(f"{payload.get('question', '')} → {decision}{comment}")
        blocks.append(bullets(rows))

    if options.include_stats:
        blocks.append(divider())
        blocks.append(heading("Статистика прогона", 2))
        done = sum(1 for s in bundle.subtasks if s.status == "done")
        reworks = sum(s.rework_count for s in bundle.subtasks)
        blocks.append(bullets([
            f"Подзадач выполнено: {done} из {len(bundle.subtasks)}",
            f"Доработок: {reworks}",
            f"Израсходовано токенов: {bundle.tokens:,}".replace(",", " "),
            f"Примерная стоимость: ${bundle.cost:.4f}",
            f"Агентов в проекте: {len(bundle.agent_names)}",
        ]))

    if bundle.files and options.include_files:
        blocks.append(heading("Файлы проекта", 2))
        blocks.append(bullets([
            f"{f.relative_to(bundle.workspace_dir)}  ({_human_size(f)})"
            for f in bundle.files[:200]
        ]))

    return blocks


def _body_blocks(raw: str) -> list[Block]:
    """Разбивает текст результата на абзацы и блоки кода.

    Полноценный парсер Markdown здесь не нужен: единственное, что важно
    не испортить, - ограждённые блоки кода.
    """
    blocks: list[Block] = []
    buffer: list[str] = []
    in_code = False
    language = ""

    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append(code("\n".join(buffer), language))
                buffer, in_code, language = [], False, ""
            else:
                if buffer:
                    blocks.append(text("\n".join(buffer).strip()))
                    buffer = []
                in_code = True
                language = stripped[3:].strip()
            continue
        buffer.append(line)

    if buffer:
        tail = "\n".join(buffer).strip()
        if tail:
            blocks.append(code(tail, language) if in_code else text(tail))
    return blocks


def _status_title(status: str) -> str:
    return {"done": "принято", "review": "на проверке", "rework": "на доработке",
            "error": "с ошибкой", "paused": "на паузе",
            "running": "выполняется"}.get(status, status)


def _when(raw: str) -> str:
    # В базе время в UTC; в документе - местное, как и "Сформировано".
    return local_time(raw, "%d.%m.%Y %H:%M") if raw else ""


def _human_size(path: Path) -> str:
    try:
        size = float(path.stat().st_size)
    except OSError:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"
