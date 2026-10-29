"""Веб-поиск и чтение страниц.

Ответ на вопрос 9: по умолчанию используется DuckDuckGo (библиотека ``ddgs``) -
она не требует ключа, поэтому поиск работает «из коробки». Если пользователь
добавит ключ Tavily или Brave, можно переключить бэкенд в настройках воркспейса.

Сетевые вызовы вынесены в поток (``asyncio.to_thread``), потому что ``ddgs``
синхронная, и блокировать общий asyncio-луп нельзя.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.tools.base import Tool, ToolContext, ToolError

MAX_RESULTS = 8
# сколько секунд ждать ответа поисковика
SEARCH_TIMEOUT = 40
MAX_PAGE_CHARS = 20_000
# сколько байт страницы скачивать не больше
MAX_DOWNLOAD_BYTES = 5_000_000
# Википедия и часть сайтов отвечают 403 клиентам без контактного адреса
# в User-Agent (https://w.wiki/4wJS), поэтому он указан явно.
USER_AGENT = "Mozilla/5.0 (compatible; AgentForge/1.2; +https://github.com/AgentForgeTeam/AgentForge)"


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Найти информацию в интернете. Возвращает список результатов: "
        "заголовок, ссылка, краткое описание. Для подробностей открой ссылку "
        "инструментом fetch_url."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "max_results": {"type": "integer", "description": "Сколько результатов вернуть",
                            "default": 5},
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        query = (kwargs.get("query") or "").strip()
        if not query:
            raise ToolError("Пустой поисковый запрос")
        try:
            n = max(1, min(int(kwargs.get("max_results") or 5), MAX_RESULTS))
        except (TypeError, ValueError):
            n = 5

        backend = ctx.search_backend
        if backend == "tavily" and ctx.search_api_key:
            search = _tavily(query, n, ctx.search_api_key)
        elif backend == "brave" and ctx.search_api_key:
            search = _brave(query, n, ctx.search_api_key)
        else:
            search = asyncio.to_thread(_duckduckgo, query, n)
        # Поисковики бывает зависают; агент не должен ждать до конца лимита задачи.
        try:
            items = await asyncio.wait_for(search, SEARCH_TIMEOUT)
        except asyncio.TimeoutError:
            raise ToolError(f"Поиск не ответил за {SEARCH_TIMEOUT} с. Попробуй другой "
                            "запрос или открой известную страницу через fetch_url.") from None

        if not items:
            return f"По запросу «{query}» ничего не найдено."
        lines = [f"Результаты поиска: {query}", ""]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}")
        return "\n".join(lines)


class WebFetchTool(Tool):
    name = "fetch_url"
    description = "Открыть веб-страницу и вернуть её основной текст без разметки."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полный URL, включая https://"},
        },
        "required": ["url"],
    }

    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        url = (kwargs.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolError("URL должен начинаться с http:// или https://")
        if not ctx.fetch_pages:
            raise ToolError("Загрузка страниц отключена в настройках воркспейса")

        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise ToolError(f"HTTP {resp.status_code} при загрузке {url}")
                    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind and not (kind.startswith("text/") or "html" in kind
                                     or "xml" in kind or "json" in kind):
                        raise ToolError(f"По ссылке не страница, а файл ({kind}) - "
                                        "его текст этим инструментом не прочитать")
                    # Ограничение объёма: ссылка на гигабайтный файл не должна
                    # выкачиваться в память целиком.
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_DOWNLOAD_BYTES:
                            break
                    encoding = resp.encoding or "utf-8"
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Не удалось загрузить страницу: {exc}") from exc

        try:
            html = bytes(body).decode(encoding, "replace")
        except LookupError:          # сервер назвал несуществующую кодировку
            html = bytes(body).decode("utf-8", "replace")
        text = await asyncio.to_thread(_extract_text, html)
        clipped = text[:MAX_PAGE_CHARS]
        tail = "\n\n(текст обрезан)" if len(text) > MAX_PAGE_CHARS else ""
        return f"Источник: {url}\n\n{clipped}{tail}"


# ---------------------------------------------------------------------------
# Бэкенды поиска
# ---------------------------------------------------------------------------


def _duckduckgo(query: str, n: int) -> list[dict]:
    """Поиск без ключа. Библиотека называется ``ddgs`` (ранее duckduckgo-search)."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        except ImportError as exc:
            raise ToolError(
                "Веб-поиск недоступен: установите пакет «ddgs» "
                "(pip install ddgs) или переключите бэкенд в настройках."
            ) from exc
    with DDGS() as ddgs:
        rows = list(ddgs.text(query, max_results=n))
    return [{"title": r.get("title", ""), "url": r.get("href") or r.get("link", ""),
             "snippet": (r.get("body") or "")[:400]} for r in rows]


async def _tavily(query: str, n: int, api_key: str) -> list[dict]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": n},
        )
    if resp.status_code >= 400:
        raise ToolError(f"Tavily: HTTP {resp.status_code}")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("content") or "")[:400]}
            for r in resp.json().get("results", [])]


async def _brave(query: str, n: int, api_key: str) -> list[dict]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise ToolError(f"Brave: HTTP {resp.status_code}")
    web = resp.json().get("web", {})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("description") or "")[:400]}
            for r in web.get("results", [])]


def _extract_text(html: str) -> str:
    """Извлекает основной текст: trafilatura -> bs4 -> грубый фолбэк."""
    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
        if extracted:
            return extracted
    except ImportError:
        pass
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        return "\n".join(line.strip() for line in soup.get_text("\n").splitlines()
                         if line.strip())
    except ImportError:
        pass
    import re

    return re.sub(r"<[^>]+>", " ", html)
