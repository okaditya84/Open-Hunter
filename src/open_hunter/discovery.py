"""Find a company's careers source.

Pipeline of increasing effort:
  1. Look at the homepage; scan for links to a known ATS (Greenhouse, Lever,
     ...). If found, we're done — the ATS API gives clean data.
  2. Otherwise collect/guess careers-page URLs and scan those for an ATS.
  3. If still nothing, hand the careers-page URLs to the generic crawler.

Detection is evidence-based: we only claim an ATS when we actually see its
URL on the company's pages, so the board token is real, not guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from .ats.base import ATSClient, all_clients
from .http_client import BlockedError, HttpClient, RobotsDisallowed
from .logging_util import get_logger
from .models import ResolvedCompany
from .utils import (
    absolutize,
    looks_like_careers_link,
    normalize_url,
    registered_domain,
)

log = get_logger(__name__)

# Common careers paths to probe when no link is found on the homepage.
_COMMON_PATHS = [
    "/careers",
    "/careers/",
    "/career",
    "/jobs",
    "/jobs/",
    "/join-us",
    "/join",
    "/company/careers",
    "/about/careers",
    "/en/careers",
    "/work-with-us",
    "/opportunities",
    "/life/careers",
]

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)]+", re.I)


@dataclass
class Discovery:
    ats_name: Optional[str] = None
    ats_token: Optional[str] = None
    careers_urls: List[str] = field(default_factory=list)
    # (url, html) pairs for the generic crawler fallback.
    pages: List[Tuple[str, str]] = field(default_factory=list)
    blocked: bool = False


def discover(http: HttpClient, resolved: ResolvedCompany) -> Discovery:
    result = Discovery()
    website = resolved.website
    if not website:
        return result

    clients = all_clients(http)

    # --- Step 1: homepage — scan static HTML for an ATS link (cheapest) ---
    home_html = _fetch(http, website, result)
    if home_html:
        result.pages.append((website, home_html))
        hit = _scan_for_ats(clients, website, home_html)
        if hit:
            result.ats_name, result.ats_token = hit
            result.careers_urls.append(website)
            return result

    # --- Step 2: guess-and-verify ATS probing ---
    # Many companies embed their ATS via JavaScript, so it never appears in
    # static HTML. We guess likely board tokens from the name/domain and
    # confirm each against the platform's own API — only accepting a token the
    # API says is real. This is verification, not blind guessing.
    hit = _probe_ats(clients, resolved.name, website)
    if hit:
        result.ats_name, result.ats_token = hit
        return result

    # --- Step 3: gather candidate careers URLs ---
    candidates = _career_candidates(website, home_html)

    for url in candidates:
        html = _fetch(http, url, result)
        if not html:
            continue
        result.careers_urls.append(url)
        result.pages.append((url, html))
        hit = _scan_for_ats(clients, url, html)
        if hit:
            result.ats_name, result.ats_token = hit
            return result

    return result


# ---------------------------------------------------------------------- #
def _fetch(http: HttpClient, url: str, result: Discovery) -> Optional[str]:
    try:
        resp = http.get(url)
    except BlockedError:
        result.blocked = True
        return None
    except RobotsDisallowed:
        return None
    if resp is None or resp.status_code >= 400:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        return None
    return resp.text


def _career_candidates(website: str, home_html: Optional[str]) -> List[str]:
    seen = []

    def add(u: str):
        u = u.strip()
        if u and u not in seen:
            seen.append(u)

    # Links on the homepage that look like careers links.
    if home_html:
        soup = BeautifulSoup(home_html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if looks_like_careers_link(href, a.get_text(" ", strip=True)):
                add(absolutize(website, href))

    # Probe common paths on the same site.
    base = website.rstrip("/")
    for path in _COMMON_PATHS:
        add(base + path)

    # Probe careers/jobs subdomains.
    dom = registered_domain(website)
    if dom:
        for sub in ("careers", "jobs"):
            add(normalize_url(f"{sub}.{dom}"))

    return seen[:20]  # keep it bounded


_STOPWORDS = {"the", "and", "inc", "ltd", "llc", "corp", "group", "co"}


def _candidate_tokens(name: str, website: str) -> List[str]:
    cands: List[str] = []
    if website:
        stem = registered_domain(website).split(".")[0]
        if stem:
            cands.append(stem)

    n = (name or "").strip().lower()
    alnum_lower = re.sub(r"[^a-z0-9]", "", n)
    alnum_orig = re.sub(r"[^A-Za-z0-9]", "", (name or "").strip())
    hyphen = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    words = [w for w in n.split() if len(w) >= 3 and w not in _STOPWORDS]
    first = words[0] if words else ""

    for t in (alnum_lower, alnum_orig, hyphen, first):
        if t:
            cands.append(t)

    seen = set()
    out = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:4]


def _probe_ats(clients, name: str, website: str) -> Optional[Tuple[str, str]]:
    tokens = _candidate_tokens(name, website)
    if not tokens:
        return None
    for client in clients:
        # Skip platforms that can't be cheaply/safely probed (e.g. Workday).
        if type(client).exists is ATSClient.exists:
            continue
        for tok in tokens:
            try:
                if client.exists(tok):
                    log.debug("probe verified %s token=%s", client.name, tok)
                    return client.name, tok
            except Exception:  # noqa: BLE001
                continue
    return None


def _scan_for_ats(clients, page_url: str, html: str) -> Optional[Tuple[str, str]]:
    """Return (ats_name, token) if any ATS URL appears in the page."""
    urls = set()

    soup = BeautifulSoup(html, "lxml")
    for tag, attr in (("a", "href"), ("iframe", "src"), ("script", "src"),
                      ("link", "href")):
        for el in soup.find_all(tag):
            val = el.get(attr)
            if val:
                urls.add(absolutize(page_url, val))

    # Also catch ATS URLs embedded in inline scripts / JSON blobs.
    for m in _URL_IN_TEXT.findall(html):
        urls.add(m)

    # The page URL itself might already be an ATS page (we redirected there).
    urls.add(page_url)

    for url in urls:
        for client in clients:
            token = client.token_from_url(url)
            if token:
                log.debug("detected %s token=%s from %s", client.name, token, url)
                return client.name, token
    return None
