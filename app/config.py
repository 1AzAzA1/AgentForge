"""Глобальная конфигурация приложения: пути, константы, настройки по умолчанию.

Все пользовательские данные хранятся ЛОКАЛЬНО в домашнем каталоге пользователя.
Каталог можно переопределить переменной окружения ``AGENTFORGE_HOME``
(старое имя ``AIORC_HOME`` тоже понимается).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Agent Forge"
APP_SLUG = "agent-forge"
# каталог данных до переименования проекта (AI Orchestrator -> Agent Forge)
LEGACY_SLUG = "ai-orchestrator"
APP_VERSION = "1.1.1"          # 1.1.1: исправления после полного прохода по коду
SCHEMA_VERSION = 2             # версия схемы SQLite (для миграций)


def _default_home() -> Path:
    """Возвращает корневой каталог данных приложения для текущей ОС."""
    env = os.environ.get("AGENTFORGE_HOME") or os.environ.get("AIORC_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    # Профили, созданные до переименования, продолжают работать: если нового
    # каталога ещё нет, а старый есть, используем старый.
    legacy = base / LEGACY_SLUG
    if not (base / APP_SLUG).exists() and legacy.exists():
        return legacy
    return base / APP_SLUG


@dataclass(frozen=True)
class Paths:
    """Набор путей, используемых приложением."""

    home: Path = field(default_factory=_default_home)

    @property
    def db_file(self) -> Path:
        return self.home / "app.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def workspaces_dir(self) -> Path:
        """Корень песочниц/рабочих файлов воркспейсов."""
        return self.home / "workspaces"

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    @property
    def config_file(self) -> Path:
        return self.home / "settings.json"

    def workspace_dir(self, workspace_id: int) -> Path:
        """Каталог конкретного воркспейса (рабочая зона агентов)."""
        return self.workspaces_dir / f"ws_{workspace_id}"

    def ensure(self) -> None:
        """Создаёт все необходимые каталоги."""
        for p in (self.home, self.logs_dir, self.workspaces_dir, self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)


PATHS = Paths()


@dataclass
class AppSettings:
    """Настройки уровня приложения (не привязаны к пользователю)."""

    language: str = "ru"           # "ru" | "en"
    # насколько активны анимации интерфейса: "full" | "reduced" | "off"
    motion: str = "full"
    last_username: str = ""
    remember_master_password: bool = False

    @classmethod
    def load(cls) -> "AppSettings":
        PATHS.ensure()
        if PATHS.config_file.exists():
            try:
                data = json.loads(PATHS.config_file.read_text("utf-8"))
                known = {f for f in cls.__dataclass_fields__}
                return cls(**{k: v for k, v in data.items() if k in known})
            except Exception:  # noqa: BLE001 - повреждённый конфиг не должен ронять старт
                pass
        return cls()

    def save(self) -> None:
        PATHS.ensure()
        PATHS.config_file.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2), "utf-8"
        )


# ---------------------------------------------------------------------------
# Значения по умолчанию для воркспейса (этапы 5-9 читают их отсюда)
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_SETTINGS: dict = {
    "human_in_the_loop": True,          # паузы в критических точках
    "hitl_confidence_threshold": 0.5,   # ниже этой самооценки агента - спросить человека
    "hitl_pause_on_milestone": False,   # пауза после каждой волны подзадач
    "summary_interval_minutes": 15,     # периодическая сводка супервайзера
    "summary_on_event": True,           # сводка при завершении подзадачи
    "supervisor_agent_id": None,        # какой агент играет роль супервайзера
    "supervisor_mode": "api",           # "api" | "local" (Ollama и т.п.)
    "supervisor_local_model": "qwen2.5:7b-instruct",
    "supervisor_local_base_url": "http://localhost:11434/v1",
    "max_rework_rounds": 2,             # сколько раз супервайзер может вернуть работу
    "anonymize_summaries": True,        # пересказ без указания авторов
    "task_token_limit": None,           # None = без лимита (вопрос 7)
    "agent_max_steps": 10,              # шагов ReAct-цикла на подзадачу
    "max_parallel_agents": 6,           # сколько агентов работают одновременно
    "tools_enabled": ["web_search", "files", "code_exec"],
    "extra_allowed_paths": [],          # доп. каталоги для файлового инструмента
    "sandbox_backend": "auto",          # "auto" | "subprocess" | "docker"
    "sandbox_timeout_sec": 30,
    "sandbox_memory_mb": 512,
    "search_backend": "duckduckgo",     # "duckduckgo" | "tavily" | "brave"
    "search_api_key": "",               # ключ Tavily/Brave, хранится зашифрованным
    "fetch_pages": True,                # скачивать и парсить страницы из выдачи
}
