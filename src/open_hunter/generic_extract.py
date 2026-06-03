"""Generic extraction for career pages that aren't on a known ATS.

Strategy, biased toward truthfulness:
  1. Render the careers page (browser if available, else raw HTML).
  2. Collect the *real* anchor links on the page.
  3. Ask the LLM which of those links are individual job postings — it may
     only choose from links we actually found, so URLs can't be fabricated.
  4. Visit each posting, render it, and have the LLM extract fields strictly
     from that page's text. The apply URL defaults to the real page URL.

If no LLM is configured, generic extraction is skipped (we won't guess).
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from .config import Config
from .crawler import BrowserCrawler
from .http_client import BlockedError, HttpClient, RobotsDisallowed
from .llm import LLMClient, LLMError
from .logging_util import get_logger
from .models import Job
from .utils import (
    absolutize,
    clean_text,
    html_to_text,
    registered_domain,
    same_registered_domain,
    truncate,
)

log = get_logger(__name__)

_MAX_JOBS_PER_COMPANY = 40
_MAX_LINKS_TO_LLM = 120

_LIST_SYSTEM = (
    "You are given the visible text of a company's careers page and a numbered "
    "list of links found on it. Identify which links point to individual job "
    "postings (not category filters, login, social media, or navigation). "
    "Respond ONLY with JSON: {\"jobs\": [{\"index\": <int>, \"title\": "
    "\"...\"}]}. Use ONLY indexes from the provided list. If there are no job "
    "postings, return {\"jobs\": []}."
)

_DETAIL_SYSTEM = (
    "You are given the plain text of a single job posting page. Extract the "
    "facts that are explicitly present. Respond ONLY with JSON of the form: "
    "{\"title\": \"\", \"location\": \"\", \"job_id\": \"\", "
    "\"posted_date\": \"YYYY-MM-DD or empty\", \"employment_type\": \"\", "
    "\"department\": \"\", \"skills\": \"comma-separated or empty\", "
    "\"apply_url\": \"absolute URL if an apply link is shown, else empty\", "
    "\"description\": \"the role description text\"}. "
    "Never invent values; leave a field empty if it is not stated."
)


class GenericExtractor:
    def __init__(
        self,
        config: Config,
        http: HttpClient,
        browser: BrowserCrawler,
        llm: Optional[LLMClient],
    ):
        self.config = config
        self.http = http
        self.browser = browser
        self.llm = llm

    def extract(
        self, company_name: str, careers_urls: List[str]
    ) -> List[Job]:
        if self.llm is None:
            log.info("no LLM configured; skipping generic extraction for %s",
                     company_name)
            return []

        collected: List[Job] = []
        seen_urls = set()

        for careers_url in careers_urls[:3]:
            html = self._get_html(careers_url)
            if not html:
                continue

            links = self._candidate_links(careers_url, html)
            if not links:
                continue

            page_text = truncate(html_to_text(html), 6000)
            postings = self._ask_for_postings(page_text, links)

            for url, title in postings:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                job = self._extract_detail(company_name, url, title)
                if job:
                    collected.append(job)
                if len(collected) >= _MAX_JOBS_PER_COMPANY:
                    return collected

        return collected

    # ------------------------------------------------------------------ #
    def _get_html(self, url: str) -> Optional[str]:
        rendered = self.browser.render(url)
        if rendered:
            return rendered
        try:
            resp = self.http.get(url)
        except (BlockedError, RobotsDisallowed):
            return None
        if resp is None or resp.status_code >= 400:
            return None
        return resp.text

    def _candidate_links(
        self, careers_url: str, html: str
    ) -> List[Tuple[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        out: List[Tuple[str, str]] = []
        seen = set()
        base_domain = registered_domain(careers_url)
        for a in soup.find_all("a", href=True):
            href = absolutize(careers_url, a["href"])
            if not href.startswith("http"):
                continue
            if href in seen:
                continue
            text = a.get_text(" ", strip=True)
            # Keep links on the same site or that clearly look job-ish.
            blob = (href + " " + text).lower()
            jobish = any(
                k in blob
                for k in ("job", "position", "opening", "role", "vacancy",
                          "apply", "career", "requisition", "/p/", "gh_jid")
            )
            if same_registered_domain(href, careers_url) or jobish:
                # Skip obvious non-postings.
                if any(
                    bad in href.lower()
                    for bad in ("mailto:", "tel:", "/privacy", "/cookie",
                                "linkedin.com", "twitter.com", "facebook.com")
                ):
                    continue
                seen.add(href)
                out.append((href, text))
            if len(out) >= _MAX_LINKS_TO_LLM:
                break
        return out

    def _ask_for_postings(
        self, page_text: str, links: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        numbered = "\n".join(
            f"{i}. {text or '(no text)'} -> {url}"
            for i, (url, text) in enumerate(links)
        )
        user = f"CAREERS PAGE TEXT:\n{page_text}\n\nLINKS:\n{numbered}"
        try:
            result = self.llm.complete_json(_LIST_SYSTEM, user)
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.debug("posting-list LLM call failed: %s", exc)
            return []

        out: List[Tuple[str, str]] = []
        for item in (result or {}).get("jobs", []):
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(links):
                url = links[idx][0]
                title = item.get("title") or links[idx][1]
                out.append((url, title))
        return out

    def _extract_detail(
        self, company_name: str, url: str, title_hint: str
    ) -> Optional[Job]:
        html = self._get_html(url)
        if not html:
            return None
        text = truncate(html_to_text(html), 12000)
        user = f"URL: {url}\n\nPAGE TEXT:\n{text}"
        try:
            data = self.llm.complete_json(_DETAIL_SYSTEM, user)
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.debug("detail LLM call failed for %s: %s", url, exc)
            return None
        if not isinstance(data, dict):
            return None

        apply_url = (data.get("apply_url") or "").strip()
        if not apply_url.startswith("http"):
            apply_url = url

        return Job(
            company_name=company_name,
            title=(data.get("title") or title_hint or "").strip(),
            location=clean_text(data.get("location", "")),
            job_id=str(data.get("job_id", "")).strip(),
            apply_url=apply_url,
            posting_url=url,
            posted_date=str(data.get("posted_date", "")).strip(),
            employment_type=clean_text(data.get("employment_type", "")),
            department=clean_text(data.get("department", "")),
            skills=clean_text(data.get("skills", "")),
            description=truncate(clean_text(data.get("description", "")), 8000),
            source="generic",
        )
