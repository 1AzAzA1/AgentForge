"""Повторы запросов к модели при временных сбоях."""

from __future__ import annotations

import httpx
import pytest

import providers.retry as retry
from providers.base import ChatMessage, CompletionResult, LLMProvider, ProviderError, Usage
from providers.factory import build_provider
from providers.retry import RetryingProvider, backoff, is_transient, suggested_delay


class Flaky(LLMProvider):
    """Отвечает ошибками из списка, потом успешно."""

    def __init__(self, errors: list[ProviderError], stream_before_error: bool = False) -> None:
        super().__init__()
        self.errors = list(errors)
        self.calls = 0
        self.stream_before_error = stream_before_error

    async def complete(self, model, messages, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return CompletionResult(text="ok", usage=Usage(1, 1))

    async def stream_complete(self, model, messages, *, on_delta=None, **kwargs):
        self.calls += 1
        if self.stream_before_error and on_delta:
            on_delta("частичный ответ", "text")
        if self.errors:
            raise self.errors.pop(0)
        if on_delta:
            on_delta("ok", "text")
        return CompletionResult(text="ok", usage=Usage(1, 1))

    async def list_models(self):
        return ["m"]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    return waits


MSG = [ChatMessage("user", "привет")]


async def test_retries_rate_limit_and_overload(no_sleep):
    inner = Flaky([ProviderError("429: quota", 429), ProviderError("503: high demand", 503)])
    result = await RetryingProvider(inner).complete("m", MSG)
    assert result.text == "ok"
    assert inner.calls == 3
    assert len(no_sleep) == 2


async def test_does_not_retry_bad_request_or_auth(no_sleep):
    for status in (400, 401, 403, 404):
        inner = Flaky([ProviderError(f"{status}: bad", status)])
        with pytest.raises(ProviderError):
            await RetryingProvider(inner).complete("m", MSG)
        assert inner.calls == 1
    assert no_sleep == []


async def test_gives_up_after_attempts(no_sleep):
    inner = Flaky([ProviderError("503: busy", 503)] * 10)
    with pytest.raises(ProviderError):
        await RetryingProvider(inner, attempts=3).complete("m", MSG)
    assert inner.calls == 3


def _network(cause: Exception) -> ProviderError:
    try:
        raise ProviderError(f"Сетевая ошибка: {cause}") from cause
    except ProviderError as exc:
        return exc


async def test_network_error_is_retried_twice(no_sleep):
    inner = Flaky([ProviderError("Сетевая ошибка: connection refused")] * 5)
    with pytest.raises(ProviderError):
        await RetryingProvider(inner).complete("m", MSG)
    assert inner.calls == 3
    refused = Flaky([_network(httpx.ConnectError("connection refused"))] * 5)
    with pytest.raises(ProviderError):
        await RetryingProvider(refused).complete("m", MSG)
    assert refused.calls == 3


async def test_dropped_connection_is_retried_like_overload(no_sleep):
    # обрыв открытого соединения через прокси: текст у httpx пустой
    inner = Flaky([_network(httpx.ReadError("")), _network(httpx.RemoteProtocolError("")),
                   _network(httpx.ReadTimeout("")), _network(httpx.ReadError(""))])
    result = await RetryingProvider(inner).complete("m", MSG)
    assert result.text == "ok" and inner.calls == 5
    assert "[ReadError]" in retry.describe(_network(httpx.ReadError("")))


async def test_stream_is_not_repeated_after_text_was_shown(no_sleep):
    inner = Flaky([ProviderError("503: busy", 503)], stream_before_error=True)
    pieces: list[str] = []
    with pytest.raises(ProviderError):
        await RetryingProvider(inner).stream_complete("m", MSG, on_delta=lambda p, k="text": pieces.append(p))
    assert inner.calls == 1


async def test_stream_is_repeated_before_first_piece(no_sleep):
    inner = Flaky([ProviderError("429: quota", 429)])
    pieces: list[str] = []
    result = await RetryingProvider(inner).stream_complete(
        "m", MSG, on_delta=lambda p, k="text": pieces.append(p))
    assert result.text == "ok" and pieces == ["ok"]


def test_server_hint_sets_the_delay():
    exc = ProviderError('429: {"retryDelay": "31s"}', 429)
    assert suggested_delay(exc) == 31
    assert suggested_delay(ProviderError("429: quota exceeded. Please retry in 24.22s.", 429)) == 24.22
    assert 31 <= backoff(1, exc) <= 32
    assert backoff(10, ProviderError("503", 503)) <= retry.MAX_DELAY + 1


async def test_daily_quota_fails_fast(no_sleep):
    daily = ProviderError("429: Quota exceeded, limit: 20. Please retry in 17h15m35.98s.", 429)
    assert suggested_delay(daily) == pytest.approx(17 * 3600 + 15 * 60 + 35.98)
    inner = Flaky([daily, daily])
    with pytest.raises(retry.QuotaExhausted, match="Квота модели исчерпана"):
        await RetryingProvider(inner).complete("m", MSG)
    assert inner.calls == 1 and no_sleep == []


def test_transient_classification():
    assert is_transient(ProviderError("x", 429))
    assert is_transient(ProviderError("Сетевая ошибка: timeout"))
    assert not is_transient(ProviderError("Gemini отклонил запрос: SAFETY"))
    assert not is_transient(ValueError("x"))


def test_factory_wraps_providers():
    provider = build_provider("gemini", "k")
    assert isinstance(provider, RetryingProvider)
    assert type(provider.inner).__name__ == "GeminiProvider"
    assert not isinstance(build_provider("openai", "k", retries=False), RetryingProvider)
