"""Точка входа Agent Forge.

Запуск приложения::

    python main.py

открывает окно на Electron (каталог ``desktop``). Окно само поднимает ядро
командой ``python main.py --serve``: это Python-процесс с агентами, базой и
WebSocket-сервером, через который окно получает состояние и шлёт действия.

Для разработки ядро можно запустить отдельно и открыть интерфейс в браузере::

    python main.py --serve --port 8765 --token dev
    cd desktop && npm run web
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Чтобы приложение запускалось из любого каталога.
sys.path.insert(0, str(ROOT))

from app.config import APP_NAME, PATHS, AppSettings  # noqa: E402
from app.i18n import set_language  # noqa: E402
from utils.logging_setup import setup_logging  # noqa: E402

log = logging.getLogger("aiorc.main")

DESKTOP = ROOT / "desktop"


def _check_dependencies() -> None:
    """Понятное сообщение вместо стектрейса, если зависимости не установлены."""
    missing: list[str] = []
    for module, package in (("websockets", "websockets"), ("httpx", "httpx"),
                            ("cryptography", "cryptography")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print(f"Не установлены зависимости: {', '.join(missing)}\n"
              f"Выполните:  pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)


def _watch_parent(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Окно закрылось: stdin процесса закрывается, и ядро завершается само."""

    def wait_eof() -> None:
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=wait_eof, name="parent-watch", daemon=True).start()


async def _serve(port: int, token: str, exit_with_parent: bool) -> None:
    from bridge.backend import Backend
    from bridge.server import run_server
    from storage.db import Database

    settings = AppSettings.load()
    set_language(settings.language)
    backend = Backend(Database(), settings)
    stop = asyncio.Event()
    if exit_with_parent:
        _watch_parent(asyncio.get_running_loop(), stop)

    def ready(actual_port: int) -> None:
        # Строку читает процесс Electron, чтобы узнать порт.
        print("AGENTFORGE_READY " + json.dumps({"port": actual_port}), flush=True)

    await run_server(backend, token, port, stop, ready)


def serve(argv: argparse.Namespace) -> int:
    _check_dependencies()
    setup_logging()
    PATHS.ensure()
    log.info("Старт ядра %s, каталог данных: %s", APP_NAME, PATHS.home)
    token = argv.token or os.environ.get("AGENTFORGE_TOKEN") or secrets.token_urlsafe(24)
    if not (argv.token or os.environ.get("AGENTFORGE_TOKEN")):
        print(f"Токен подключения: {token}", file=sys.stderr, flush=True)
    try:
        asyncio.run(_serve(argv.port, token, argv.exit_with_parent))
    except KeyboardInterrupt:
        pass
    return 0


def electron_binary() -> Path | None:
    dist = DESKTOP / "node_modules" / "electron" / "dist"
    names = {"win32": "electron.exe", "darwin": "Electron.app/Contents/MacOS/Electron"}
    path = dist / names.get(sys.platform, "electron")
    return path if path.exists() else None


def launch() -> int:
    """Открывает окно приложения; ядро окно запустит само."""
    electron = electron_binary()
    if electron is None or not (DESKTOP / "dist" / "index.html").exists():
        print("Интерфейс ещё не собран. Выполните один раз:\n"
              "    cd desktop\n    npm install\n    npm run build\n"
              "и снова запустите python main.py", file=sys.stderr)
        return 1
    # Ядру нужен stdout, чтобы сообщить окну порт. У pythonw.exe (ярлык без
    # консоли) его может не быть, поэтому ядро запускается через python.exe
    # из того же окружения; консоль окно всё равно скрывает.
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe" and (python.parent / "python.exe").exists():
        python = python.parent / "python.exe"
    env = dict(os.environ, AGENTFORGE_PYTHON=str(python))
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return subprocess.call([str(electron), str(DESKTOP)], env=env)  # noqa: S603


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--serve", action="store_true", help="запустить только ядро (WebSocket)")
    parser.add_argument("--port", type=int, default=0, help="порт ядра, 0 - любой свободный")
    parser.add_argument("--token", default="", help="токен подключения окна")
    parser.add_argument("--exit-with-parent", action="store_true",
                        help="завершиться, когда закроется stdin (окно Electron)")
    args = parser.parse_args()
    return serve(args) if args.serve else launch()


if __name__ == "__main__":
    sys.exit(main())
