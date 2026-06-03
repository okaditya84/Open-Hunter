"""Small, dependency-light helpers used across the pipeline."""

from __future__ import annotations

import re
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlparse

import tldextract
from bs4 import BeautifulSoup

# A platform-independent tldextract that won't hit the network on every call.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

# Domains that are aggregators / social / infra — never a company's own site.
NON_COMPANY_DOMAINS = {
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "crunchbase.com",
    "wikipedia.org",
    "bloomberg.com",
    "ziprecruiter.com",
    "monster.com",
    "wellfound.com",
    "angel.co",
    "github.com",
    "medium.com",
    "google.com",
    "apple.com",  # app store links, not careers
    "play.google.com",
}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def registered_domain(url_or_host: str) -> str:
    """Return e.g. 'example.com' from any URL or host string."""
    if not url_or_host:
        return ""
    host = url_or_host
    if "://" in url_or_host:
        host = urlparse(url_or_host).netloc
    ext = _EXTRACT(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return host.lower()


def is_aggregator(url_or_host: str) -> bool:
    return registered_domain(url_or_host) in NON_COMPANY_DOMAINS


def same_registered_domain(a: str, b: str) -> bool:
    return bool(a) and registered_domain(a) == registered_domain(b)


def absolutize(base_url: str, href: str) -> str:
    return urljoin(base_url, (href or "").strip())


_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Turn an HTML fragment into clean, readable plain text."""
    if not html:
        return ""
    # Decode HTML entities first: some ATSes (e.g. Greenhouse) return content
    # that is HTML-escaped, so tags arrive as "&lt;div&gt;" rather than "<div>".
    html = unescape(html)
    # If it still has no tags, it's genuine plain text — just tidy whitespace.
    if "<" not in html:
        return clean_text(html)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # Preserve list/line structure a little.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    text = soup.get_text(separator="\n")
    return clean_text(text)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = text.replace("\r", "\n")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def to_iso_date(value: Optional[str]) -> str:
    """Normalize a variety of date strings to 'YYYY-MM-DD' (or '')."""
    if not value:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    # Epoch milliseconds/seconds.
    if value.isdigit():
        from datetime import datetime, timezone

        ts = int(value)
        if ts > 1_000_000_000_000:  # ms
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
        except (ValueError, OSError):
            return ""
    # ISO-ish strings: take the date portion.
    iso = value.replace("Z", "+00:00")
    from datetime import datetime

    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d %b %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(iso, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: grab a yyyy-mm-dd substring if present.
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return m.group(0) if m else ""


CAREER_HINTS = (
    "career",
    "careers",
    "jobs",
    "job",
    "join-us",
    "join_us",
    "joinus",
    "work-with-us",
    "we-are-hiring",
    "hiring",
    "vacancies",
    "openings",
    "opportunities",
    "life-at",
    "employment",
)


def looks_like_careers_link(href: str, text: str = "") -> bool:
    blob = f"{href} {text}".lower()
    return any(hint in blob for hint in CAREER_HINTS)
