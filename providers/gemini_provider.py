"""Провайдер Google Gemini (generativeLanguage API).

Особенности, скрытые внутри класса:
* роли называются ``user``/``model``, системный промпт - ``systemInstruction``;
* ключ передаётся заголовком ``x-goog-api-key``;
* инструменты описываются как ``functionDeclarations``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

from providers.base import (
    ChatMessage,
    CompletionResult,
    DeltaHandler,
    LLMProvider,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
    is_chat_model,
)

# сколько токенов сверх лимита ответа оставить "думающим" моделям: у Gemini
# ``maxOutputTokens`` включает размышления, и при лимите агента в 2048
# модель могла потратить всё на них и вернуть пустой ответ
THINKING_HEADROOM = 8192

# почему кандидат остался пустым (``finishReason``) и что сказать человеку
_EMPTY_REASONS = {
    "MAX_TOKENS": "модель израсходовала лимит токенов на размышления и не успела "
                  "ответить. Увеличьте «Макс. токенов» у агента",
    "SAFETY": "ответ заблокирован фильтром безопасности Google",
    "PROHIBITED_CONTENT": "ответ заблокирован фильтром безопасности Google",
    "BLOCKLIST": "ответ заблокирован фильтром безопасности Google",
    "SPII": "ответ заблокирован: в нём были персональные данные",
    "RECITATION": "ответ заблокирован: он повторял защищённый текст",
    "MALFORMED_FUNCTION_CALL": "модель сформировала некорректный вызов инструмента",
}


class GeminiProvider(LLMProvider):
    key = "gemini"

    def __init__(self, api_key: str = "", base_url: str = "",
                 timeout: float = 120.0) -> None:
        super().__init__(
            api_key, base_url or "https://generativelanguage.googleapis.com/v1beta", timeout
        )
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _split(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        previous_tool = False
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
                continue
            if m.role == "tool":
                part = {"functionResponse": {"name": m.name or "tool",
                                             "response": {"result": m.content}}}
                # Ответы на несколько вызовов одного хода Gemini ждёт одним
                # сообщением: число частей должно совпасть с числом вызовов.
                if previous_tool:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                previous_tool = True
                continue
            previous_tool = False
            if m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls:
                    call: dict[str, Any] = {"functionCall": {"name": tc.name, "args": tc.arguments}}
                    if tc.signature:
                        # Новые модели требуют вернуть подпись вместе с вызовом.
                        call["thoughtSignature"] = tc.signature
                    parts.append(call)
                contents.append({"role": "model", "parts": parts or [{"text": " "}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.content or " "}]})
        return "\n\n".join(p for p in system_parts if p), contents

    def _payload(self, messages: list[ChatMessage], temperature: float, max_tokens: int,
                 tools: list[ToolSpec] | None, model: str = "") -> dict[str, Any]:
        system, contents = self._split(messages)
        config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if _thinks(model):
            # Размышления входят в maxOutputTokens: без запаса модель может
            # потратить весь лимит на них и не выдать ответа. Сами мысли
            # просим присылать, чтобы экран "Выполнение" показывал их вживую.
            config["maxOutputTokens"] = max_tokens + THINKING_HEADROOM
            config["thinkingConfig"] = {"includeThoughts": True}
        if _is_gemini3(model):
            # Для Gemini 3 Google просит не трогать температуру: ниже 1.0
            # модель склонна зацикливаться (а супервайзер ставит 0.2).
            config.pop("temperature", None)
        payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    {"name": t.name, "description": t.description,
                     "parameters": _schema(t.parameters)}
                    for t in tools
                ]
            }]
        return payload

    @staticmethod
    def _call(part: dict[str, Any]) -> ToolCall:
        fc = part.get("functionCall") or {}
        return ToolCall(id=uuid.uuid4().hex[:12], name=fc.get("name", ""),
                        arguments=ToolCall.parse_args(fc.get("args") or {}),
                        signature=part.get("thoughtSignature") or "")

    async def stream_complete(self, model: str, messages: list[ChatMessage], *,
                              temperature: float = 0.7, max_tokens: int = 2048,
                              tools: list[ToolSpec] | None = None,
                              on_delta: DeltaHandler | None = None) -> CompletionResult:
        """``streamGenerateContent`` в режиме SSE.

        Каждый чанк - полноценный ответ с частью ``parts``; вызовы функций
        приходят целиком, а ``usageMetadata`` в последнем чанке содержит
        итоговый расход.
        """
        import json as _json

        payload = self._payload(messages, temperature, max_tokens, tools, model)
        url = f"{self.base_url}/models/{_model_id(model)}:streamGenerateContent?alt=sse"
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: dict[str, Any] = {}
        finish_reason = ""
        block_reason = ""
        try:
            async with self._http().stream("POST", url, headers=self._headers(),
                                           json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ProviderError(_error_text(resp), resp.status_code)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = _json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    usage = chunk.get("usageMetadata") or usage
                    block_reason = ((chunk.get("promptFeedback") or {}).get("blockReason")
                                    or block_reason)
                    for candidate in chunk.get("candidates") or []:
                        finish_reason = candidate.get("finishReason") or finish_reason
                        for part in (candidate.get("content") or {}).get("parts", []):
                            if "functionCall" in part:
                                calls.append(self._call(part))
                            elif part.get("text"):
                                kind = "reasoning" if part.get("thought") else "text"
                                if kind == "text":
                                    text_parts.append(part["text"])
                                if on_delta:
                                    on_delta(part["text"], kind)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Сетевая ошибка: {exc}") from exc
        text = "".join(text_parts)
        _raise_if_empty(text, calls, finish_reason, block_reason)
        return CompletionResult(
            text=text, tool_calls=calls,
            usage=Usage(int(usage.get("promptTokenCount", 0)),
                        int(usage.get("candidatesTokenCount", 0))
                        + int(usage.get("thoughtsTokenCount", 0))),
            finish_reason=finish_reason, model=model,
        )

    async def complete(self, model: str, messages: list[ChatMessage], *,
                       temperature: float = 0.7, max_tokens: int = 2048,
                       tools: list[ToolSpec] | None = None) -> CompletionResult:
        payload = self._payload(messages, temperature, max_tokens, tools, model)
        url = f"{self.base_url}/models/{_model_id(model)}:generateContent"
        try:
            resp = await self._http().post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Сетевая ошибка: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(_error_text(resp), resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"Сервер вернул не JSON: {resp.text[:200]}") from exc
        candidate = (data.get("candidates") or [{}])[0]
        text_parts, calls = [], []
        for part in (candidate.get("content") or {}).get("parts", []):
            if "functionCall" in part:
                calls.append(self._call(part))
            elif "text" in part and not part.get("thought"):
                # Рассуждение "думающих" моделей в ответ не входит.
                text_parts.append(part["text"])
        text = "".join(text_parts)
        _raise_if_empty(text, calls, candidate.get("finishReason", ""),
                        (data.get("promptFeedback") or {}).get("blockReason", ""))
        u = data.get("usageMetadata") or {}
        return CompletionResult(
            text=text,
            tool_calls=calls,
            # Токены рассуждения оплачиваются как выходные.
            usage=Usage(int(u.get("promptTokenCount", 0)),
                        int(u.get("candidatesTokenCount", 0))
                        + int(u.get("thoughtsTokenCount", 0))),
            finish_reason=candidate.get("finishReason", ""),
            model=model,
            raw=data,
        )

    async def list_models(self) -> list[str]:
        try:
            resp = await self._http().get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(f"Сетевая ошибка: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(_error_text(resp), resp.status_code)
        names = []
        for m in resp.json().get("models", []):
            name = (m.get("name") or "").removeprefix("models/")
            methods = m.get("supportedGenerationMethods") or ["generateContent"]
            # Озвучка, картинки и эмбеддинги агенту не подходят, а список и так
            # длинный: оставляем только модели для диалога.
            if "generateContent" in methods and is_chat_model(name):
                names.append(name)
        return sorted(names)


def _model_id(model: str) -> str:
    """Имя модели для адреса запроса: префикс «models/» уже есть в пути."""
    return (model or "").strip().removeprefix("models/")


def _thinks(model: str) -> bool:
    """Модель рассуждает перед ответом: семейства 2.5 и 3.x, алиасы latest."""
    name = _model_id(model).lower()
    return bool(re.match(r"gemini-(2\.5|[3-9])", name)) or (
        name.startswith("gemini-") and name.endswith("-latest"))


def _is_gemini3(model: str) -> bool:
    name = _model_id(model).lower()
    return bool(re.match(r"gemini-[3-9]", name)) or (
        name.startswith("gemini-") and name.endswith("-latest"))


def _raise_if_empty(text: str, calls: list[ToolCall], finish_reason: str,
                    block_reason: str) -> None:
    """Пустой ответ без объяснения выглядел бы как «агент ничего не сделал».

    Gemini в таких случаях сообщает причину отдельным полем: лимит токенов
    ушёл на размышления, сработал фильтр безопасности и т.п. Её и отдаём.
    """
    if text.strip() or calls:
        return
    if block_reason:
        raise ProviderError(f"Gemini отклонил запрос: {block_reason}")
    reason = (finish_reason or "").upper()
    if reason in _EMPTY_REASONS:
        raise ProviderError(f"Gemini: {_EMPTY_REASONS[reason]} ({reason})")


# ключи JSON Schema, которые понимает ``functionDeclarations``; прочие
# (``default``, ``additionalProperties``, ``$schema``...) Gemini отвергает
_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties",
                "required", "items", "minimum", "maximum", "minItems", "maxItems"}


def _schema(node: Any) -> Any:
    """Приводит JSON Schema инструмента к подмножеству, которое принимает Gemini."""
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _schema(sub) for name, sub in value.items()}
        elif key == "items":
            out[key] = _schema(value)
        else:
            out[key] = value
    return out


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{resp.status_code}: {err.get('message', resp.text[:200])}"
    except Exception:  # noqa: BLE001
        return f"{resp.status_code}: {resp.text[:200]}"
