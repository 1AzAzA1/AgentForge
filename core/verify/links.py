"""Проверка ссылок в ответе агента: выдуманные источники.

Модель охотно ссылается на страницы, которых нет, или на настоящие страницы,
где нет того, что она якобы оттуда взяла. Ссылка считается подтверждённой,
если страница открывается и (когда агент привёл цитату) цитата на ней
действительно есть. Агенты приводят источники блоком

    ИСТОЧНИКИ:
    - https://example.org/page | "точная цитата со страницы"

но ссылки ищутся и в остальном тексте ответа.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx

TIMEOUT = 20
MAX_BYTES = 3_000_000
USER_AGENT = "AgentForge/1.2 (link check; +https://github.com/AgentForgeTeam/AgentForge)"
# какая доля слов цитаты должна найтись подряд на странице, чтобы считать её найденной
QUOTE_MATCH = 0.8

# Апостроф и скобки в адресе законны (Wikipedia: McDonald's,
# Python_(programming_language)), поэтому они не обрывают ссылку.
_URL_RE = re.compile(r"https?://[^\s<>\"`|\]]+", re.IGNORECASE)
_QUOTE_RE = re.compile(r"[\"«“„]([^\"»”“]{8,500})[\"»”“]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class Citation:
    url: str
    quote: str = ""


@dataclass
class LinkCheck:
    url: str
    quote: str = ""
    status: int | None = None
    reachable: bool = False
    quote_found: bool | None = None
    error: str = ""

    @property
    def confirmed(self) -> bool:
        """Ссылка настоящая и (если была цитата) подтверждает её."""
        return self.reachable and self.quote_found is not False


@dataclass
class LinkReport:
    checks: list[LinkCheck] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def fabricated(self) -> int:
        return sum(1 for c in self.checks if not c.confirmed)

    @property
    def fabricated_share(self) -> float | None:
        return self.fabricated / self.total if self.total else None

    def as_dict(self) -> dict:
        return {"total": self.total, "fabricated": self.fabricated,
                "fabricated_share": self.fabricated_share,
                "checks": [c.__dict__ for c in self.checks]}


def _clean_url(url: str) -> str:
    """Убирает знаки препинания, прилипшие к концу ссылки в тексте."""
    while url:
        stripped = url.rstrip(".,;:!?»”'\"")
        # Закрывающая скобка - часть адреса, только если у неё есть пара.
        if stripped.endswith(")") and stripped.count("(") < stripped.count(")"):
            stripped = stripped[:-1]
        if stripped == url:
            break
        url = stripped
    return url


def extract_citations(text: str) -> list[Citation]:
    """Ссылки из ответа с цитатами, если они стоят в той же строке."""
    seen: dict[str, Citation] = {}
    for line in (text or "").splitlines():
        urls = [_clean_url(u) for u in _URL_RE.findall(line)]
        if not urls:
            continue
        quote_match = _QUOTE_RE.search(line)
        quote = quote_match.group(1).strip() if quote_match else ""
        for url in urls:
            if url not in seen:
                seen[url] = Citation(url, quote if len(urls) == 1 else "")
            elif quote and not seen[url].quote:
                seen[url].quote = quote
    return list(seen.values())


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def quote_in_text(quote: str, page_text: str) -> bool:
    """Есть ли цитата на странице, без учёта регистра, пунктуации и пробелов.

    Модель редко цитирует слово в слово, поэтому ищется самый длинный общий
    кусок подряд идущих слов: цитата считается найденной, если он покрывает
    не меньше ``QUOTE_MATCH`` её слов.
    """
    q = _words(quote)
    if not q:
        return True
    page = " " + " ".join(_words(page_text)) + " "
    if " " + " ".join(q) + " " in page:
        return True
    need = max(1, int(len(q) * QUOTE_MATCH + 0.999))
    for size in range(len(q), need - 1, -1):
        for start in range(0, len(q) - size + 1):
            if " " + " ".join(q[start:start + size]) + " " in page:
                return True
    return False


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    async with client.stream("GET", url) as resp:
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        kind = resp.headers.get("content-type", "")
        status = resp.status_code
        encoding = resp.encoding or "utf-8"
    try:
        html = bytes(body).decode(encoding, "replace")
    except LookupError:
        html = bytes(body).decode("utf-8", "replace")
    if "html" in kind or "xml" in kind or not kind:
        from core.tools.web_search import _extract_text

        return status, await asyncio.to_thread(_extract_text, html)
    return status, html


async def check_citation(client: httpx.AsyncClient, citation: Citation) -> LinkCheck:
    check = LinkCheck(citation.url, citation.quote)
    try:
        status, text = await _fetch_text(client, citation.url)
    except Exception as exc:  # noqa: BLE001 - любая ошибка значит "не открылась"
        check.error = f"{type(exc).__name__}: {exc}"[:300]
        return check
    check.status = status
    check.reachable = status < 400
    if check.reachable and citation.quote:
        check.quote_found = quote_in_text(citation.quote, text)
    return check


async def check_links(text: str, concurrency: int = 4) -> LinkReport:
    """Проверяет все ссылки ответа и возвращает отчёт."""
    citations = extract_citations(text)
    if not citations:
        return LinkReport()
    limit = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        async def one(c: Citation) -> LinkCheck:
            async with limit:
                return await check_citation(client, c)

        checks = await asyncio.gather(*(one(c) for c in citations))
    return LinkReport(list(checks))
