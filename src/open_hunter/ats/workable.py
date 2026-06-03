"""Workable — apply.workable.com public widget API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_SUB_RE = re.compile(r"https?://([a-zA-Z0-9_-]+)\.workable\.com", re.I)
_PATH_RE = re.compile(r"apply\.workable\.com/([a-zA-Z0-9_-]+)", re.I)


class WorkableClient(ATSClient):
    name = "workable"

    def token_from_url(self, url: str) -> Optional[str]:
        if "workable.com" not in url:
            return None
        m = _PATH_RE.search(url)
        if m and m.group(1) not in {"api", "j"}:
            return m.group(1)
        m = _SUB_RE.search(url)
        if m and m.group(1) not in {"www", "apply", "api"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        data = self.http.get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{token}",
            check_robots=False,
        )
        return bool(data and "jobs" in data)

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = (
            f"https://apply.workable.com/api/v1/widget/accounts/{token}"
            "?details=true"
        )
        data = self.http.get_json(url, check_robots=False)
        if not data or "jobs" not in data:
            return []

        jobs: List[Job] = []
        for raw in data["jobs"]:
            if raw.get("state") and raw.get("state") != "published":
                continue
            loc = raw.get("location", {}) or {}
            location = ", ".join(
                p
                for p in (
                    loc.get("city"),
                    loc.get("region"),
                    loc.get("country"),
                )
                if p
            )
            if loc.get("workplace_type") == "remote" or raw.get("remote"):
                location = (location + " (Remote)").strip()
            parts = [
                html_to_text(raw.get("description", "")),
                html_to_text(raw.get("requirements", "")),
                html_to_text(raw.get("benefits", "")),
            ]
            description = "\n\n".join(p for p in parts if p)
            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("title", "").strip(),
                    location=location,
                    job_id=str(raw.get("shortcode") or raw.get("id", "")),
                    apply_url=raw.get("application_url")
                    or raw.get("shortlink")
                    or raw.get("url", ""),
                    posting_url=raw.get("url", ""),
                    posted_date=to_iso_date(
                        raw.get("published_on") or raw.get("created_at", "")
                    ),
                    employment_type=raw.get("employment_type", ""),
                    department=raw.get("department", "") or "",
                    description=truncate(description, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = WorkableClient
