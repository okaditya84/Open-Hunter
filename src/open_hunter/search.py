"""Free web search via DuckDuckGo (no API key required).

Used by the resolver to turn a bare company name into candidate websites.
We keep this isolated behind one function so it can be swapped for a paid
search API later without touching the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .logging_util import get_logger

log = get_logger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def web_search(query: str, max_results: int = 8) -> List[SearchResult]:
    """Return search results, or an empty list if search is unavailable."""
    try:
        from ddgs import DDGS
    except ImportError:  # pragma: no cover
        log.warning("ddgs not installed; web search disabled")
        return []

    results: List[SearchResult] = []
    try:
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=max_results):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("href", "") or item.get("url", ""),
                        snippet=item.get("body", ""),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.debug("web search failed for %r: %s", query, exc)
        return []

    return [r for r in results if r.url]
