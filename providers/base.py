"""Единый интерфейс LLM-провайдера.

Все провайдеры (OpenAI, Anthropic, Gemini, Groq, OpenRouter, Ollama, HF)
приводятся к одному набору типов, чтобы ядро агентов ничего не знало
о различиях в их HTTP-API - включая формат tool-calling.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

# получатель фрагментов при потоковой генерации: (текст, вид). Вид -
# ``"text"`` для ответа модели или ``"reasoning"`` для рассуждения
# моделей, которые отдают его отдельно (DeepSeek-R1, Claude thinking и т.п.)
DeltaHandler = Callable[[str, str], None]


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов, если провайдер не прислал расход.

    Лучше посчитать приблизительно, чем записать ноль: нулевой расход
    незаметно обходил бы лимиты бюджета.
    """
    return max(1, len(text or "") // 4) if text else 0


# признаки моделей, которые не ведут диалог: озвучка, распознавание речи,
# картинки, видео, эмбеддинги, модерация. Агенту они не подходят, и в
# выпадающем списке только мешают выбрать рабочую модель.
_NOT_CHAT = re.compile(
    r"embed|whisper|tts|transcri|speech|audio|realtime|live|image|imagen|veo|"
    r"lyria|dall-e|moderation|guard|orpheus|robotics|computer-use|deep-research|"
    r"antigravity|omni|davinci|babbage|aqa", re.IGNORECASE)


def is_chat_model(name: str) -> bool:
    """Годится ли модель для агента: текст на входе, текст и вызовы на выходе."""
    return bool(name) and not _NOT_CHAT.search(name)


@dataclass
class ToolCall:
    """Запрос модели на вызов инструмента."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # служебная подпись вызова, которую провайдер просит вернуть вместе с
    # ним в следующем запросе (``thoughtSignature`` у Gemini)
    signature: str = ""

    @staticmethod
    def parse_args(raw: Any) -> dict[str, Any]:
        """Аргументы вызова всегда словарь: инструменты получают их как ``**kwargs``."""
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {"_raw": str(raw)}
        return data if isinstance(data, dict) else {"_raw": str(raw)}


@dataclass
class ChatMessage:
    """Сообщение диалога в нейтральном формате."""

    role: str                         # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""            # для role="tool"
    name: str = ""


@dataclass
class ToolSpec:
    """Описание инструмента в формате JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Usage:
    """Расход токенов за один вызов."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class CompletionResult:
    """Результат одного обращения к модели."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Ошибка обращения к провайдеру с человекочитаемым сообщением."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMProvider(ABC):
    """Базовый класс провайдера.

    Реализации обязаны быть потокобезопасными в пределах одного asyncio-лупа
    и не хранить состояние диалога - вся история приходит в ``messages``.
    """

    # строковый идентификатор пресета (см. providers/presets.py)
    key: str = ""

    def __init__(self, api_key: str = "", base_url: str = "", timeout: float = 120.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult:
        """Однократный вызов модели."""

    async def stream_complete(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[ToolSpec] | None = None,
        on_delta: DeltaHandler | None = None,
    ) -> CompletionResult:
        """Вызов модели с потоковой выдачей текста.

        Результат тот же, что у ``complete`` (текст, вызовы инструментов,
        расход), но по ходу генерации каждый фрагмент текста передаётся в
        ``on_delta`` - так интерфейс показывает рассуждение агента вживую.
        Реализация по умолчанию делает обычный вызов и отдаёт текст целиком:
        провайдер без стриминга просто покажет ответ разом.
        """
        result = await self.complete(model, messages, temperature=temperature,
                                     max_tokens=max_tokens, tools=tools)
        if on_delta and result.text:
            on_delta(result.text, "text")
        return result

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Список доступных моделей (используется в UI и для проверки ключа)."""

    async def stream(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Потоковая генерация. По умолчанию - эмуляция через ``complete``."""
        result = await self.complete(
            model, messages, temperature=temperature, max_tokens=max_tokens
        )
        yield result.text

    async def test(self) -> list[str]:
        """Проверка соединения: возвращает список моделей либо бросает ProviderError."""
        return await self.list_models()

    async def aclose(self) -> None:
        """Освобождение ресурсов (HTTP-клиента)."""
