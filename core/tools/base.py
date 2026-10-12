"""Базовая инфраструктура инструментов агентов (tool-calling).

Модель прав простая и явная: у каждого запуска агента есть ``ToolContext``
с корнем рабочего каталога и списком дополнительно разрешённых путей.
Инструмент обязан валидировать любой путь через ``ToolContext.resolve``,
иначе обращение за пределы песочницы просто не состоится.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from providers.base import ToolSpec


class ToolError(RuntimeError):
    """Ошибка инструмента, которую не стыдно показать модели дословно."""


@dataclass
class ToolContext:
    """Границы, в которых агенту позволено действовать."""

    workspace_dir: Path
    agent_id: int | None = None
    subtask_id: int | None = None
    # дополнительные каталоги, явно разрешённые пользователем в настройках
    extra_allowed_paths: list[Path] = field(default_factory=list)
    # параметры песочницы
    sandbox_backend: str = "auto"
    sandbox_timeout_sec: int = 30
    sandbox_memory_mb: int = 512
    allow_network_in_sandbox: bool = False
    # настройки веб-поиска
    search_backend: str = "duckduckgo"
    search_api_key: str = ""
    fetch_pages: bool = True

    def roots(self) -> list[Path]:
        return [self.workspace_dir.resolve(), *[p.resolve() for p in self.extra_allowed_paths]]

    def resolve(self, raw_path: str, must_exist: bool = False) -> Path:
        """Приводит путь к абсолютному и проверяет, что он внутри разрешённых корней.

        Защищает от ``../``, абсолютных путей и симлинков наружу.
        """
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"Некорректный путь: {raw_path} ({exc})") from exc

        for root in self.roots():
            if resolved == root or root in resolved.parents:
                if must_exist and not resolved.exists():
                    raise ToolError(f"Файл не найден: {raw_path}")
                return resolved
        raise ToolError(
            f"Доступ запрещён: путь «{raw_path}» вне разрешённых каталогов. "
            "Разрешены только рабочий каталог воркспейса и каталоги, "
            "добавленные пользователем в настройках."
        )


class Tool(ABC):
    """Инструмент, доступный агенту через tool-calling."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)

    @abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        """Выполняет инструмент и возвращает текстовый результат для модели."""


class ToolRegistry:
    """Реестр инструментов; агент получает только разрешённое ему подмножество."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self, allowed: list[str]) -> list[ToolSpec]:
        return [t.spec() for n, t in self._tools.items() if n in allowed]

    def names(self) -> list[str]:
        return list(self._tools)

    async def invoke(self, name: str, ctx: ToolContext, **kwargs: Any) -> str:
        """Вызывает инструмент, превращая любые ошибки в текст для модели."""
        tool = self.get(name)
        if tool is None:
            return f"ОШИБКА: инструмент «{name}» недоступен."
        try:
            return await tool.run(ctx, **kwargs)
        except ToolError as exc:
            return f"ОШИБКА ИНСТРУМЕНТА: {exc}"
        except Exception as exc:  # noqa: BLE001 - модель должна узнать о сбое
            return f"ОШИБКА ИНСТРУМЕНТА ({type(exc).__name__}): {exc}"


# групповые имена из шаблонов ролей и настроек воркспейса -> реальные инструменты
TOOL_GROUPS: dict[str, list[str]] = {
    "files": ["read_file", "write_file", "list_dir"],
    "web_search": ["web_search", "fetch_url"],
}


def expand_tool_names(names) -> list[str]:
    """Разворачивает групповые имена, сохраняя порядок и убирая повторы."""
    out: list[str] = []
    for name in names or []:
        for real in TOOL_GROUPS.get(name, [name]):
            if real not in out:
                out.append(real)
    return out


def default_registry() -> ToolRegistry:
    """Собирает стандартный набор инструментов."""
    from core.tools.code_exec import CodeExecTool
    from core.tools.files import FileReadTool, FileWriteTool, ListDirTool
    from core.tools.web_search import WebFetchTool, WebSearchTool

    reg = ToolRegistry()
    for tool in (WebSearchTool(), WebFetchTool(), FileReadTool(),
                 FileWriteTool(), ListDirTool(), CodeExecTool()):
        reg.register(tool)
    return reg
