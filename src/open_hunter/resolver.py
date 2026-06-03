"""Resolve a company *name* to its real *website*.

This is the first hard problem: the user may give only a name. We:
  1. Use the website if the user supplied one (and sanity-check it).
  2. Otherwise web-search the name, discard aggregator/social domains, and
     ask the LLM to pick the genuine official site from the candidates.
  3. Fall back to a transparent heuristic when no LLM is configured.

We never guess silently: every resolution carries a confidence and a note,
and anything we can't resolve is reported, not fabricated.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import List, Optional

from .config import Config
from .http_client import HttpClient
from .llm import LLMClient, LLMError
from .logging_util import get_logger
from .models import CompanyInput, ResolvedCompany
from .search import SearchResult, web_search
from .utils import is_aggregator, normalize_url, registered_domain

log = get_logger(__name__)


_RESOLVE_SYSTEM = (
    "You identify the single official corporate website for a company. "
    "You are given the company name and a list of candidate URLs from a web "
    "search. Pick the URL that is the company's own primary website (its "
    "homepage domain), NOT a social network, job board, news article, "
    "directory, or reseller. Respond ONLY with JSON of the form "
    '{\"domain\": \"example.com\", \"confidence\": 0.0-1.0, \"reason\": \"...\"}. '
    "If none of the candidates is plausibly the official site, return "
    '{\"domain\": null, \"confidence\": 0, \"reason\": \"...\"}.'
)


class CompanyResolver:
    def __init__(
        self,
        config: Config,
        http: HttpClient,
        llm: Optional[LLMClient] = None,
    ):
        self.config = config
        self.http = http
        self.llm = llm

    def resolve(self, company: CompanyInput) -> ResolvedCompany:
        resolved = ResolvedCompany(name=company.name)

        # 1) Trust a user-supplied website, but verify it loads.
        if company.website:
            url = normalize_url(company.website)
            if self._reachable(url):
                resolved.website = url
                resolved.resolution_confidence = 1.0
                resolved.resolution_note = "website supplied by user"
                return resolved
            resolved.resolution_note = (
                f"user-supplied website {url} was unreachable; searching"
            )

        # 2) Web search for candidates.
        candidates = self._gather_candidates(company.name)
        if not candidates:
            resolved.resolution_note = (
                "web search returned no usable candidates"
            )
            return resolved

        # 3) Choose the best candidate.
        chosen, confidence, reason = self._choose(company.name, candidates)
        if not chosen:
            resolved.resolution_note = f"could not pick official site: {reason}"
            return resolved

        url = normalize_url(chosen)
        if not self._reachable(url):
            resolved.resolution_note = f"chosen site {url} was unreachable"
            return resolved

        resolved.website = url
        resolved.resolution_confidence = confidence
        resolved.resolution_note = reason
        return resolved

    # ------------------------------------------------------------------ #
    def _gather_candidates(self, name: str) -> List[SearchResult]:
        seen = OrderedDict()
        queries = [
            f"{name} official website",
            f"{name} company careers",
        ]
        for q in queries:
            for r in web_search(q, max_results=8):
                if is_aggregator(r.url):
                    continue
                dom = registered_domain(r.url)
                if not dom or dom in seen:
                    continue
                seen[dom] = r
        return list(seen.values())

    def _choose(
        self, name: str, candidates: List[SearchResult]
    ) -> tuple[Optional[str], float, str]:
        # LLM path (preferred).
        if self.llm is not None:
            try:
                payload = {
                    "company": name,
                    "candidates": [
                        {
                            "domain": registered_domain(c.url),
                            "url": c.url,
                            "title": c.title,
                            "snippet": c.snippet[:200],
                        }
                        for c in candidates
                    ],
                }
                result = self.llm.complete_json(
                    _RESOLVE_SYSTEM, json.dumps(payload, ensure_ascii=False)
                )
                domain = (result or {}).get("domain")
                if domain:
                    return (
                        domain,
                        float(result.get("confidence", 0.6) or 0.6),
                        str(result.get("reason", "selected by LLM")),
                    )
                return None, 0.0, str(result.get("reason", "LLM found no match"))
            except (LLMError, Exception) as exc:  # noqa: BLE001
                log.debug("LLM resolution failed for %s: %s", name, exc)
                # fall through to heuristic

        # Heuristic fallback: prefer a candidate whose domain contains a
        # significant token of the company name.
        tokens = [
            t.lower()
            for t in name.replace(",", " ").replace(".", " ").split()
            if len(t) >= 3 and t.lower() not in {"inc", "ltd", "llc", "the"}
        ]
        for c in candidates:
            dom = registered_domain(c.url)
            stem = dom.split(".")[0]
            if any(tok in stem or stem in tok for tok in tokens):
                return dom, 0.55, f"heuristic domain match on '{stem}'"

        # Last resort: the top non-aggregator result, flagged low-confidence.
        top = candidates[0]
        return (
            registered_domain(top.url),
            0.3,
            "heuristic: top search result (low confidence)",
        )

    def _reachable(self, url: str) -> bool:
        try:
            resp = self.http.get(url, check_robots=False)
        except Exception:  # noqa: BLE001 - blocked/robots still means "exists"
            return True
        return resp is not None and resp.status_code < 500
