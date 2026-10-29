"""Повторы запросов к модели при временных сбоях.

Провайдеры регулярно отвечают 429 (кончилась квота в минуту), 503 (модель
перегружена) или рвут соединение. Это не повод ронять подзадачу: запрос
повторяется с растущей паузой. Если сервер сам сказал, сколько ждать
(``retryDelay`` у Gemini, заголовок ``Retry-After``), берётся его срок.

Потоковый вызов повторяется, только пока модель не успела выдать ни одного
фрагмента: иначе в интерфейсе текст задвоился бы.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from providers.base import CompletionResult, LLMProvider, ProviderError

log = logging.getLogger("aiorc.retry")

# статусы, при которых повтор имеет смысл
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 5
BASE_DELAY = 2.0
MAX_DELAY = 60.0
# Если сервер просит ждать дольше, это не перегрузка, а исчерпанная суточная
# квота: повторять бесполезно, лучше сразу честно сказать об этом.
QUOTA_DELAY = 300.0

_DELAY_RE = re.compile(
    r"retry(?:Delay|[ -]after|\s+in)?[\"']?\s*[:=]?\s*[\"']?"
    r"((?:\d+h)?(?:\d+m)?\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)([hm]?)")


class QuotaExhausted(ProviderError):
    """Квота модели кончилась надолго (обычно до следующих суток)."""


def is_transient(exc: BaseException) -> bool:
    """Сбой временный: перегрузка, лимит в минуту, обрыв сети."""
    if not isinstance(exc, ProviderError):
        return False
    if exc.status in RETRY_STATUSES:
        return True
    return exc.status is None and str(exc).startswith("Сетевая ошибка")


def cannot_connect(exc: ProviderError) -> bool:
    """Сервер не принял подключение (или причина неизвестна).

    Так бывает, когда локальная модель не запущена: ждать минуту бессмысленно.
    Обрыв уже открытого соединения (разрыв, таймаут чтения, ошибка протокола)
    обычно временный и повторяется как перегрузка.
    """
    return not isinstance(exc.__cause__, httpx.TransportError) or isinstance(
        exc.__cause__, httpx.ConnectError)


def describe(exc: ProviderError) -> str:
    """Текст ошибки для журнала; у части обрывов httpx он пустой."""
    cause = exc.__cause__
    return f"{exc} [{type(cause).__name__}]" if cause is not None else str(exc)


def suggested_delay(exc: BaseException) -> float | None:
    """Сколько ждать, если сервер подсказал это в тексте ошибки."""
    match = _DELAY_RE.search(str(exc))
    if not match:
        return None
    seconds = 0.0
    for value, unit in _PART_RE.findall(match.group(1)):
        seconds += float(value) * {"h": 3600, "m": 60}.get(unit, 1)
    return seconds


def backoff(attempt: int, exc: BaseException) -> float:
    hinted = suggested_delay(exc)
    if hinted is not None:
        return min(hinted + random.uniform(0, 1), MAX_DELAY)
    return min(BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, 1), MAX_DELAY)


class RetryingProvider(LLMProvider):
    """Обёртка над провайдером: те же методы, но с повторами."""

    def __init__(self, inner: LLMProvider, attempts: int = MAX_ATTEMPTS) -> None:
        super().__init__(inner.api_key, inner.base_url, inner.timeout)
        self.inner = inner
        self.key = inner.key
        self.attempts = max(1, attempts)

    async def _retry(self, call, *, streamed: list[bool] | None = None):
        for attempt in range(1, self.attempts + 1):
            try:
                return await call()
            except ProviderError as exc:
                last = attempt == self.attempts
                # Неудачное подключение повторяем дважды: если сервер не
                # запущен (локальная Ollama), ждать минуту бессмысленно.
                network_again = exc.status is None and attempt >= 3 and cannot_connect(exc)
                if last or network_again or not is_transient(exc) or (streamed and streamed[0]):
                    raise
                hinted = suggested_delay(exc)
                if hinted is not None and hinted > QUOTA_DELAY:
                    hours = hinted / 3600
                    raise QuotaExhausted(
                        f"Квота модели исчерпана, сервер просит подождать "
                        f"{hours:.0f} ч. Выберите другую модель или ключ. ({exc})",
                        exc.status) from exc
                delay = backoff(attempt, exc)
                log.warning("Временный сбой модели (%s), повтор %s/%s через %.0f с",
                            describe(exc), attempt + 1, self.attempts, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def complete(self, model: str, messages, **kwargs: Any) -> CompletionResult:
        return await self._retry(lambda: self.inner.complete(model, messages, **kwargs))

    async def stream_complete(self, model: str, messages, *, on_delta=None,
                              **kwargs: Any) -> CompletionResult:
        streamed = [False]

        def relay(piece: str, kind: str = "text") -> None:
            streamed[0] = True
            if on_delta is not None:
                on_delta(piece, kind)

        return await self._retry(
            lambda: self.inner.stream_complete(model, messages, on_delta=relay, **kwargs),
            streamed=streamed)

    async def list_models(self) -> list[str]:
        return await self._retry(self.inner.list_models)

    async def test(self) -> list[str]:
        # Проверка ключа должна честно показать ошибку сразу, без ожиданий.
        return await self.inner.test()

    async def aclose(self) -> None:
        await self.inner.aclose()

    def __getattr__(self, name: str):
        # Особые методы конкретного провайдера доступны как раньше.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)
