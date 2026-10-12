"""Подключение к локальной SQLite-БД и применение схемы.

Соединение одно на процесс (``check_same_thread=False``), запись защищена
мьютексом - этого достаточно, потому что вся работа с БД идёт из одного
asyncio-лупа, а фоновые потоки обращаются к ней редко.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.config import PATHS, SCHEMA_VERSION

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    """Единый формат времени для всех таблиц (ISO-8601, UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_time(iso: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Переводит сохранённую UTC-метку в местное время для показа человеку."""
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime(fmt)


class Database:
    """Тонкая обёртка над sqlite3 с удобными хелперами."""

    _instance: "Database | None" = None

    def __init__(self, path: Path | None = None) -> None:
        PATHS.ensure()
        self.path = path or PATHS.db_file
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    # singleton
    @classmethod
    def instance(cls) -> "Database":
        if cls._instance is None:
            cls._instance = Database()
        return cls._instance

    # миграции
    def _migrate(self) -> None:
        """Применяет schema.sql и записывает версию схемы."""
        with self._lock:
            self.conn.executescript(_SCHEMA_FILE.read_text("utf-8"))
            cur = self.conn.execute("PRAGMA user_version")
            current = cur.fetchone()[0]
            if current != SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()

    # базовые операции
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Несколько изменений одним коммитом: либо применяются все, либо ни одно.

        Внутри блока писать нужно через возвращённое соединение, а не через
        ``execute``: тот коммитит каждую команду по отдельности.
        """
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
            except BaseException:
                # Упавшая команда оставляет открытой неявную транзакцию, и
                # следующий ``transaction()`` споткнулся бы на её ``BEGIN``.
                self.conn.rollback()
                raise
            return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            try:
                self.conn.executemany(sql, seq)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """INSERT с возвратом нового id."""
        return int(self.execute(sql, params).lastrowid or 0)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
