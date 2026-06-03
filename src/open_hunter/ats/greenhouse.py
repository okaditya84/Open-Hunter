"""Greenhouse — boards-api.greenhouse.io public JSON board API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_URL_RE = re.compile(
    r"(?:boards|job-boards|boards-api)\.greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?([a-zA-Z0-9_-]+)",
    re.I,
)
_FOR_PARAM_RE = re.compile(r"[?&]for=([a-zA-Z0-9_-]+)", re.I)


class GreenhouseClient(ATSClient):
    name = "greenhouse"

    def token_from_url(self, url: str) -> Optional[str]:
        if "greenhouse.io" not in url and "grnh.se" not in url:
            return None
        m = _FOR_PARAM_RE.search(url)
        if m:
            return m.group(1)
        m = _URL_RE.search(url)
        if m and m.group(1) not in {"v1", "embed"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        data = self.http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}",
            check_robots=False,
        )
        return bool(data and (data.get("name") or "jobs" in data))

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = (
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
            "?content=true"
        )
        data = self.http.get_json(url, check_robots=False)
        if not data or "jobs" not in data:
            return []

        jobs: List[Job] = []
        for raw in data["jobs"]:
            content = raw.get("content", "") or ""
            description = html_to_text(content)
            departments = ", ".join(
                d.get("name", "") for d in raw.get("departments", []) if d
            )
            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("title", "").strip(),
                    location=(raw.get("location") or {}).get("name", ""),
                    job_id=str(raw.get("id", "")),
                    apply_url=raw.get("absolute_url", ""),
                    posting_url=raw.get("absolute_url", ""),
                    posted_date=to_iso_date(
                        raw.get("first_published") or raw.get("updated_at")
                    ),
                    department=departments,
                    description=truncate(description, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = GreenhouseClient
