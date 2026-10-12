"""Выбор моделей и особенности актуальных API провайдеров (сентябрь 2026).

Каждый тест закрепляет поведение, без которого модель у провайдера просто
не работает: Gemini 2.x закрыты для новых ключей, у Gemini 3 размышления
съедают лимит ответа, флагманы GPT-6 не вызывают инструменты через Chat
Completions.
"""

from __future__ import annotations

import json

import httpx
import pytest

from providers.base import ChatMessage, ProviderError, ToolSpec, is_chat_model
from providers.gemini_provider import THINKING_HEADROOM, GeminiProvider
from providers.openai_compat import OpenAICompatProvider
from providers.presets import PRESETS
from ui.bridge.c_agents import ordered_models

TOOL = ToolSpec("read_file", "читает файл", {"type": "object", "properties": {}})
MESSAGES = [ChatMessage("system", "Ты агент."), ChatMessage("user", "задача")]


def _sse(events: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()


def _gemini(body: bytes) -> GeminiProvider:
    provider = GeminiProvider("k", "https://example.test/v1beta")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    return provider


# рекомендованные модели


def test_gemini_suggestions_work_for_new_keys():
    suggested = PRESETS["gemini"].suggested_models
    assert suggested and suggested[0].startswith("gemini-3")
    # 2.0 отключены, 2.5 закрыты для новых ключей: предлагать их нельзя.
    assert not [m for m in suggested if m.startswith(("gemini-2", "gemini-1"))]


def test_every_provider_offers_several_models():
    for key, preset in PRESETS.items():
        if key != "custom":
            assert len(preset.suggested_models) >= 4, key


def test_recommended_models_come_first_and_junk_is_hidden():
    available = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-3.5-flash-lite",
                 "gemini-3.8-flash", "gemini-3.8-flash-tts", "gemini-embedding-001"]
    ordered = ordered_models(["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-9"],
                             available)
    assert ordered[:2] == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    assert "gemini-9" not in ordered                 # у ключа такой модели нет
    assert "gemini-3.8-flash-tts" not in ordered and "gemini-embedding-001" not in ordered
    assert ordered_models(["a", "b"], []) == ["a", "b"]


def test_chat_model_filter():
    assert is_chat_model("gpt-6-luna") and is_chat_model("qwen/qwen3.8-27b")
    for name in ("whisper-1", "gpt-4o-mini-tts", "text-embedding-3-large", "gpt-image-2",
                 "gemini-3.8-live", "omni-moderation-latest"):
        assert not is_chat_model(name), name


# Gemini


def test_gemini3_payload_leaves_room_for_thinking_and_keeps_temperature_default():
    payload = GeminiProvider("k")._payload(MESSAGES, 0.2, 2048, None, "gemini-3.8-flash")
    config = payload["generationConfig"]
    assert config["maxOutputTokens"] == 2048 + THINKING_HEADROOM
    assert config["thinkingConfig"] == {"includeThoughts": True}
    assert "temperature" not in config               # Google: ниже 1.0 зацикливается


def test_gemini_older_model_keeps_plain_config():
    config = GeminiProvider("k")._payload(MESSAGES, 0.2, 2048, None,
                                          "gemini-2.0-flash")["generationConfig"]
    assert config == {"temperature": 0.2, "maxOutputTokens": 2048}


async def test_gemini_empty_answer_explains_token_limit():
    provider = _gemini(_sse([
        {"candidates": [{"content": {"parts": [{"text": "думаю", "thought": True}]}}]},
        {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]},
    ]))
    with pytest.raises(ProviderError, match="лимит токенов"):
        await provider.stream_complete("gemini-3.8-flash", MESSAGES)
    await provider.aclose()


async def test_gemini_blocked_prompt_is_reported():
    provider = _gemini(_sse([{"promptFeedback": {"blockReason": "SAFETY"}}]))
    with pytest.raises(ProviderError, match="SAFETY"):
        await provider.stream_complete("gemini-3.8-flash", MESSAGES)
    await provider.aclose()


async def test_gemini_model_list_hides_non_chat_models():
    body = json.dumps({"models": [
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
    ]}).encode()
    provider = _gemini(body)
    assert await provider.list_models() == ["gemini-3.8-flash"]
    await provider.aclose()


# OpenAI


def _openai() -> OpenAICompatProvider:
    provider = OpenAICompatProvider("k")
    provider.key = "openai"
    return provider


def test_gpt6_luna_calls_tools_without_reasoning():
    payload = _openai()._payload("gpt-6-luna", MESSAGES, 0.7, 1000, [TOOL])
    assert payload["reasoning_effort"] == "none"
    assert "temperature" not in payload and payload["max_completion_tokens"] == 1000
    plain = _openai()._payload("gpt-6-luna", MESSAGES, 0.7, 1000, None)
    assert "reasoning_effort" not in plain           # без инструментов рассуждает как обычно


def test_gpt6_flagship_with_tools_gets_a_clear_error():
    with pytest.raises(ProviderError, match="Responses API"):
        _openai()._payload("gpt-6-astra", MESSAGES, 0.7, 1000, [TOOL])
    assert _openai()._payload("gpt-6-astra", MESSAGES, 0.7, 1000, None)["model"] == "gpt-6-astra"


def test_reasoning_models_are_recognised_by_version():
    for model in ("gpt-5.4-mini", "gpt-6-sol", "o3"):
        assert "temperature" not in _openai()._payload(model, MESSAGES, 0.7, 100, None), model
    for model in ("gpt-4.1", "gpt-4o-mini"):
        assert _openai()._payload(model, MESSAGES, 0.7, 100, None)["temperature"] == 0.7, model
