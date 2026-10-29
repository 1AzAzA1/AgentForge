"""Проверка ссылок в ответах агентов: разбор, поиск цитат, сетевые ответы."""

from __future__ import annotations

import httpx

from core.verify import links
from core.verify.links import Citation, LinkCheck, LinkReport, extract_citations, quote_in_text

ANSWER = """Итог: население Парижа около 2,1 млн человек.

ИСТОЧНИКИ:
- https://en.wikipedia.org/wiki/Paris | "The City of Paris has an estimated population of 2,102,650"
- https://example.org/nowhere.
Ещё упоминание https://en.wikipedia.org/wiki/Paris без цитаты.
"""


def test_extracts_urls_quotes_and_dedupes():
    found = extract_citations(ANSWER)
    assert [c.url for c in found] == ["https://en.wikipedia.org/wiki/Paris",
                                      "https://example.org/nowhere"]
    assert found[0].quote.startswith("The City of Paris")
    assert found[1].quote == ""


def test_urls_with_apostrophes_and_brackets_stay_whole():
    text = ("- https://en.wikipedia.org/wiki/McDonald's | \"Founded in 1940\"\n"
            "См. (https://en.wikipedia.org/wiki/Python_(programming_language)).\n"
            "Источник: https://example.org/a?b=1).")
    urls = [c.url for c in extract_citations(text)]
    assert urls == ["https://en.wikipedia.org/wiki/McDonald's",
                    "https://en.wikipedia.org/wiki/Python_(programming_language)",
                    "https://example.org/a?b=1"]


def test_quote_matching_ignores_case_and_punctuation():
    page = "Paris. The city of Paris has an estimated population of 2,102,650 residents."
    assert quote_in_text("the City of Paris has an estimated population of 2,102,650", page)
    # Одно лишнее слово в конце цитаты не мешает: совпадает 80% слов подряд.
    assert quote_in_text("The City of Paris has an estimated population of 2,102,650 people", page)
    assert not quote_in_text("Paris has a population of ten million", page)
    assert quote_in_text("", page)


def test_report_counts_fabricated_links():
    report = LinkReport([
        LinkCheck("a", reachable=True),
        LinkCheck("b", "quote", reachable=True, quote_found=False),
        LinkCheck("c", reachable=False, error="404"),
        LinkCheck("d", "q", reachable=True, quote_found=True),
    ])
    assert report.total == 4 and report.fabricated == 2
    assert report.fabricated_share == 0.5
    assert LinkReport().fabricated_share is None


async def test_check_with_fake_transport(monkeypatch):
    pages = {
        "https://good.test/a": (200, "<html><body><p>The answer is forty two exactly.</p></body></html>"),
        "https://gone.test/b": (404, "not found"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = pages.get(str(request.url), (500, ""))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(links.httpx, "AsyncClient", client_factory)
    text = ('ИСТОЧНИКИ:\n- https://good.test/a | "the answer is forty two"\n'
            '- https://good.test/a2\n- https://gone.test/b')
    report = await links.check_links(text)
    by_url = {c.url: c for c in report.checks}
    assert by_url["https://good.test/a"].confirmed
    assert by_url["https://good.test/a"].quote_found is True
    assert not by_url["https://gone.test/b"].reachable
    assert not by_url["https://good.test/a2"].confirmed
    assert report.fabricated == 2


def test_citation_defaults():
    assert Citation("u").quote == ""


def test_page_requests_name_the_project_for_wikipedia():
    # Без контактного адреса Википедия отвечает 403, и агент не видит страницу.
    from core.tools.web_search import USER_AGENT

    assert "github.com/AgentForgeTeam/AgentForge" in USER_AGENT
    assert "github.com/AgentForgeTeam/AgentForge" in links.USER_AGENT


async def test_hanging_search_is_cut_off(monkeypatch, tmp_path):
    import time

    import pytest

    from core.tools import web_search
    from core.tools.base import ToolContext, ToolError

    monkeypatch.setattr(web_search, "SEARCH_TIMEOUT", 0.2)
    monkeypatch.setattr(web_search, "_duckduckgo", lambda q, n: time.sleep(2) or [])
    with pytest.raises(ToolError, match="не ответил"):
        await web_search.WebSearchTool().run(ToolContext(workspace_dir=tmp_path), query="x")
