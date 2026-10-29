"""Фабрика провайдеров и расчёт стоимости вызовов.

Провайдеры почти никогда не возвращают цену - только токены. Поэтому
стоимость считается на клиенте по таблице ``pricing.json``
(USD за 1 млн токенов). Для локальных моделей стоимость равна нулю.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from providers.base import LLMProvider
from providers.presets import preset

_PRICING_FILE = Path(__file__).with_name("pricing.json")


def build_provider(provider_key: str, api_key: str = "",
                   base_url: str = "", timeout: float = 120.0,
                   retries: bool = True) -> LLMProvider:
    """Создаёт экземпляр провайдера по ключу пресета.

    Импорт реализаций ленивый: расчёт стоимости и работа с пресетами не должны
    тянуть за собой httpx, если сетевой вызов в этом сценарии не нужен.
    По умолчанию провайдер обёрнут повторами при временных сбоях (429, 503,
    обрыв сети), см. ``providers/retry.py``.
    """
    from providers.anthropic_provider import AnthropicProvider
    from providers.gemini_provider import GeminiProvider
    from providers.openai_compat import OpenAICompatProvider
    from providers.retry import RetryingProvider

    p = preset(provider_key)
    url = base_url or p.base_url
    prov: LLMProvider
    if p.api_style == "anthropic":
        prov = AnthropicProvider(api_key, url, timeout)
    elif p.api_style == "gemini":
        prov = GeminiProvider(api_key, url, timeout)
    else:
        extra: dict[str, str] = {}
        if provider_key == "openrouter":
            # OpenRouter просит идентифицировать приложение.
            extra = {"HTTP-Referer": "https://localhost/agent-forge",
                     "X-Title": "Agent Forge"}
        prov = OpenAICompatProvider(api_key, url, timeout, extra_headers=extra)
        prov.key = provider_key
    return RetryingProvider(prov) if retries else prov


@lru_cache(maxsize=1)
def _pricing() -> dict:
    try:
        return json.loads(_PRICING_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def reload_pricing() -> None:
    """Сбрасывает кэш - используется после ручного редактирования таблицы цен."""
    _pricing.cache_clear()


def model_price(provider_key: str, model: str) -> tuple[float, float]:
    """Возвращает (цена_входа, цена_выхода) в USD за 1 млн токенов.

    Поиск идёт от точного совпадения к префиксному: ``gpt-4o-2024-11-20``
    подхватит цену ``gpt-4o``.
    """
    data = _pricing()
    if preset(provider_key).local:
        return (0.0, 0.0)
    table: dict = data.get(provider_key, {})
    if model in table:
        row = table[model]
        return float(row[0]), float(row[1])
    best: tuple[str, list] | None = None
    for name, row in table.items():
        if model.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, row)
    if best:
        return float(best[1][0]), float(best[1][1])
    default = data.get("_default", [0.0, 0.0])
    return float(default[0]), float(default[1])


def estimate_cost(provider_key: str, model: str,
                  tokens_in: int, tokens_out: int) -> float:
    """Приблизительная стоимость вызова в USD."""
    p_in, p_out = model_price(provider_key, model)
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000.0
