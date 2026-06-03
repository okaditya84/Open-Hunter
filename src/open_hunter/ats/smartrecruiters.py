"""SmartRecruiters — api.smartrecruiters.com public postings API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_URL_RE = re.compile(
    r"(?:careers|jobs)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I
)
_API_RE = re.compile(
    r"api\.smartrecruiters\.com/v1/companies/([a-zA-Z0-9_-]+)", re.I
)


class SmartRecruitersClient(ATSClient):
    name = "smartrecruiters"

    def token_from_url(self, url: str) -> Optional[str]:
        if "smartrecruiters.com" not in url:
            return None
        m = _API_RE.search(url) or _URL_RE.search(url)
        if m:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        data = self.http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1",
            check_robots=False,
        )
        return bool(data and "content" in data)

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        jobs: List[Job] = []
        offset = 0
        limit = 100
        while True:
            url = (
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                f"?limit={limit}&offset={offset}"
            )
            data = self.http.get_json(url, check_robots=False)
            if not data or "content" not in data:
                break
            content = data["content"]
            for raw in content:
                jobs.append(self._build(token, company_name, raw))
            offset += limit
            if offset >= data.get("totalFound", 0) or not content:
                break
        return jobs

    def _build(self, token: str, company_name: str, raw: dict) -> Job:
        loc = raw.get("location", {}) or {}
        location = ", ".join(
            p for p in (loc.get("city"), loc.get("region"), loc.get("country"))
            if p
        )
        job_id = str(raw.get("id", ""))
        apply_url = f"https://jobs.smartrecruiters.com/{token}/{job_id}"

        # Fetch full posting for the description + qualifications.
        description = ""
        detail = self.http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}",
            check_robots=False,
        )
        if detail:
            sections = (detail.get("jobAd", {}) or {}).get("sections", {}) or {}
            parts = []
            for key in ("companyDescription", "jobDescription", "qualifications"):
                sec = sections.get(key, {}) or {}
                text = sec.get("text", "")
                if text:
                    parts.append(html_to_text(text))
            description = "\n\n".join(parts)
            apply_url = detail.get("applyUrl", apply_url) or apply_url

        return Job(
            company_name=company_name,
            title=raw.get("name", "").strip(),
            location=location,
            job_id=job_id,
            apply_url=apply_url,
            posting_url=apply_url,
            posted_date=to_iso_date(raw.get("releasedDate", "")),
            employment_type=(raw.get("typeOfEmployment", {}) or {}).get(
                "label", ""
            ),
            department=(raw.get("department", {}) or {}).get("label", ""),
            description=truncate(description, 8000),
            source=self.name,
        )


CLIENT = SmartRecruitersClient
