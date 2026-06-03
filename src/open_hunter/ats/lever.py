"""Lever — api.lever.co/v0 public postings API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_URL_RE = re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([a-zA-Z0-9_-]+)", re.I)


class LeverClient(ATSClient):
    name = "lever"

    def token_from_url(self, url: str) -> Optional[str]:
        if "lever.co" not in url:
            return None
        m = _URL_RE.search(url)
        if m and m.group(1) not in {"v0", "postings"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        resp = self.http.get(
            f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1",
            check_robots=False,
        )
        return resp is not None and resp.status_code == 200

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        data = self.http.get_json(url, check_robots=False)
        if not isinstance(data, list):
            return []

        jobs: List[Job] = []
        for raw in data:
            categories = raw.get("categories", {}) or {}
            description = html_to_text(
                raw.get("descriptionPlain") or raw.get("description") or ""
            )
            # Lever exposes structured "lists" (responsibilities, etc.).
            extras = []
            for block in raw.get("lists", []) or []:
                extras.append(block.get("text", ""))
                extras.append(html_to_text(block.get("content", "")))
            if extras:
                description = (description + "\n\n" + "\n".join(extras)).strip()

            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("text", "").strip(),
                    location=categories.get("location", ""),
                    job_id=str(raw.get("id", "")),
                    apply_url=raw.get("applyUrl") or raw.get("hostedUrl", ""),
                    posting_url=raw.get("hostedUrl", ""),
                    posted_date=to_iso_date(str(raw.get("createdAt", ""))),
                    employment_type=categories.get("commitment", ""),
                    department=categories.get("team", "")
                    or categories.get("department", ""),
                    description=truncate(description, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = LeverClient
