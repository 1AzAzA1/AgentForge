#!/usr/bin/env bash
# Запуск Agent Forge на Linux и macOS.
# При первом запуске создаёт виртуальное окружение, ставит зависимости ядра,
# устанавливает и собирает окно (Node.js).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Не найден интерпретатор Python. Установите Python 3.11 или новее." >&2
    exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
    echo "Не найден Node.js. Установите Node.js 20 или новее: https://nodejs.org/" >&2
    exit 1
fi

VERSION=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
REQUIRED=$("$PYTHON" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [ "$REQUIRED" != "1" ]; then
    echo "Нужен Python 3.11 или новее, найден $VERSION." >&2
    echo "Укажите другой интерпретатор: PYTHON=python3.12 ./run.sh" >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Создаю виртуальное окружение (.venv)..."
    "$PYTHON" -m venv .venv
    ./.venv/bin/python -m pip install --upgrade pip --quiet
    echo "Устанавливаю зависимости ядра, это займёт пару минут..."
    ./.venv/bin/pip install -r requirements.txt
fi

if [ ! -d "desktop/node_modules" ]; then
    echo "Устанавливаю окно приложения (npm install)..."
    npm --prefix desktop install
fi
if [ ! -d "desktop/node_modules/electron/dist" ]; then
    node desktop/node_modules/electron/install.js
fi
if [ ! -f "desktop/dist/index.html" ]; then
    echo "Собираю окно..."
    npm --prefix desktop run build
fi

exec ./.venv/bin/python main.py "$@"
