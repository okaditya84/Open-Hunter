"""Ashby — api.ashbyhq.com/posting-api public job-board API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_PATH_RE = re.compile(r"(?:jobs|app)\.ashbyhq\.com/([a-zA-Z0-9_.\-]+)", re.I)
_SUB_RE = re.compile(r"https?://([a-zA-Z0-9_-]+)\.ashbyhq\.com", re.I)


class AshbyClient(ATSClient):
    name = "ashby"

    def token_from_url(self, url: str) -> Optional[str]:
        if "ashbyhq.com" not in url:
            return None
        m = _PATH_RE.search(url)
        if m:
            return m.group(1).split("/")[0]
        m = _SUB_RE.search(url)
        if m and m.group(1) not in {"jobs", "app", "api", "www"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        data = self.http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{token}",
            check_robots=False,
        )
        return bool(data and "jobs" in data)

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = (
            f"https://api.ashbyhq.com/posting-api/job-board/{token}"
            "?includeCompensation=true"
        )
        data = self.http.get_json(url, check_robots=False)
        if not data or "jobs" not in data:
            return []

        jobs: List[Job] = []
        for raw in data["jobs"]:
            description = html_to_text(
                raw.get("descriptionPlain") or raw.get("descriptionHtml") or ""
            )
            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("title", "").strip(),
                    location=raw.get("location", "")
                    or ("Remote" if raw.get("isRemote") else ""),
                    job_id=str(raw.get("id", "")),
                    apply_url=raw.get("applyUrl") or raw.get("jobUrl", ""),
                    posting_url=raw.get("jobUrl", ""),
                    posted_date=to_iso_date(raw.get("publishedAt", "")),
                    employment_type=raw.get("employmentType", ""),
                    department=raw.get("department", "")
                    or raw.get("team", ""),
                    description=truncate(description, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = AshbyClient
