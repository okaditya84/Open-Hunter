"""BambooHR — {token}.bamboohr.com/careers public JSON endpoints."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_SUB_RE = re.compile(r"https?://([a-zA-Z0-9_-]+)\.bamboohr\.com", re.I)


class BambooHRClient(ATSClient):
    name = "bamboohr"

    def token_from_url(self, url: str) -> Optional[str]:
        if "bamboohr.com" not in url:
            return None
        m = _SUB_RE.search(url)
        if m and m.group(1) not in {"www", "api"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        data = self.http.get_json(
            f"https://{token}.bamboohr.com/careers/list", check_robots=False
        )
        return bool(data and "result" in data)

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = f"https://{token}.bamboohr.com/careers/list"
        data = self.http.get_json(url, check_robots=False)
        if not data or "result" not in data:
            return []

        jobs: List[Job] = []
        for raw in data["result"]:
            job_id = str(raw.get("id", ""))
            loc = raw.get("location", {}) or {}
            location = ", ".join(
                p
                for p in (loc.get("city"), loc.get("state"), loc.get("country"))
                if p
            )
            if raw.get("isRemote") in (True, "yes", 1):
                location = (location + " (Remote)").strip()
            apply_url = f"https://{token}.bamboohr.com/careers/{job_id}"

            description = ""
            detail = self.http.get_json(
                f"https://{token}.bamboohr.com/careers/{job_id}/detail",
                check_robots=False,
            )
            if detail:
                jr = (detail.get("result", {}) or {}).get("jobOpening", {}) or {}
                description = html_to_text(jr.get("description", ""))

            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("jobOpeningName", "").strip(),
                    location=location,
                    job_id=job_id,
                    apply_url=apply_url,
                    posting_url=apply_url,
                    posted_date=to_iso_date(raw.get("datePosted", "")),
                    employment_type=raw.get("employmentStatusLabel", ""),
                    department=raw.get("departmentLabel", ""),
                    description=truncate(description, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = BambooHRClient
