"""Recruitee — {token}.recruitee.com public offers API."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_SUB_RE = re.compile(r"https?://([a-zA-Z0-9_-]+)\.recruitee\.com", re.I)


class RecruiteeClient(ATSClient):
    name = "recruitee"

    def token_from_url(self, url: str) -> Optional[str]:
        if "recruitee.com" not in url:
            return None
        m = _SUB_RE.search(url)
        if m and m.group(1) not in {"www", "api"}:
            return m.group(1)
        return None

    def exists(self, token: str) -> bool:
        # Require at least one real offer: some generic subdomains answer 200
        # with an empty/placeholder list, which would shadow the true ATS.
        data = self.http.get_json(
            f"https://{token}.recruitee.com/api/offers/", check_robots=False
        )
        return bool(data and len(data.get("offers", []) or []) > 0)

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        url = f"https://{token}.recruitee.com/api/offers/"
        data = self.http.get_json(url, check_robots=False)
        if not data or "offers" not in data:
            return []

        jobs: List[Job] = []
        for raw in data["offers"]:
            if raw.get("status") and raw.get("status") != "published":
                continue
            description = html_to_text(raw.get("description", ""))
            requirements = html_to_text(raw.get("requirements", ""))
            full = description
            if requirements:
                full = (full + "\n\nRequirements:\n" + requirements).strip()
            location = raw.get("location") or ", ".join(
                p for p in (raw.get("city"), raw.get("country")) if p
            )
            apply_url = raw.get("careers_apply_url") or raw.get("careers_url", "")
            jobs.append(
                Job(
                    company_name=company_name,
                    title=raw.get("title", "").strip(),
                    location=location or "",
                    job_id=str(raw.get("id", "")),
                    apply_url=apply_url,
                    posting_url=raw.get("careers_url", ""),
                    posted_date=to_iso_date(raw.get("published_at", "")),
                    employment_type=raw.get("employment_type_code", "")
                    or raw.get("category_code", ""),
                    department=raw.get("department", "") or "",
                    description=truncate(full, 8000),
                    source=self.name,
                )
            )
        return jobs


CLIENT = RecruiteeClient
