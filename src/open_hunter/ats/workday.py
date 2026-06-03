"""Workday — *.myworkdayjobs.com CXS JSON API.

Workday URLs look like:
    https://{tenant}.{dc}.myworkdayjobs.com/{lang}/{site}
The machine-readable API lives under:
    https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
We encode the token as "tenant::dc::site".
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urlparse

from ..models import Job
from ..utils import html_to_text, to_iso_date, truncate
from .base import ATSClient

_HOST_RE = re.compile(r"^([a-zA-Z0-9_-]+)\.([a-z0-9]+)\.myworkdayjobs\.com$", re.I)
_LANG_RE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")


class WorkdayClient(ATSClient):
    name = "workday"

    def token_from_url(self, url: str) -> Optional[str]:
        if "myworkdayjobs.com" not in url:
            return None
        parsed = urlparse(url if "://" in url else "https://" + url)
        m = _HOST_RE.match(parsed.netloc)
        if not m:
            return None
        tenant, dc = m.group(1), m.group(2)
        segments = [s for s in parsed.path.split("/") if s]
        # Drop a leading language code such as "en-US".
        if segments and _LANG_RE.match(segments[0]):
            segments = segments[1:]
        if not segments:
            return None
        site = segments[0]
        return f"{tenant}::{dc}::{site}"

    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        try:
            tenant, dc, site = token.split("::")
        except ValueError:
            return []

        base = f"https://{tenant}.{dc}.myworkdayjobs.com"
        list_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        jobs: List[Job] = []
        offset = 0
        limit = 20
        total = None
        while True:
            body = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }
            resp = self.http.post(
                list_url, json=body, headers=headers, check_robots=False
            )
            if resp is None or resp.status_code != 200:
                break
            try:
                data = resp.json()
            except ValueError:
                break

            postings = data.get("jobPostings", []) or []
            if total is None:
                total = data.get("total", len(postings))
            if not postings:
                break

            for raw in postings:
                jobs.append(self._build(base, tenant, site, company_name, raw))

            offset += limit
            if offset >= (total or 0):
                break
            if offset > 2000:  # safety cap
                break
        return jobs

    def _build(self, base, tenant, site, company_name, raw) -> Job:
        external_path = raw.get("externalPath", "")
        posting_url = f"{base}{('/' + site) if site else ''}{external_path}"
        bullets = raw.get("bulletFields", []) or []
        job_id = str(bullets[0]) if bullets else ""

        title = raw.get("title", "").strip()
        location = raw.get("locationsText", "")
        posted_date = ""
        description = ""
        apply_url = posting_url

        if external_path:
            detail = self.http.get_json(
                f"{base}/wday/cxs/{tenant}/{site}{external_path}",
                headers={"Accept": "application/json"},
                check_robots=False,
            )
            info = (detail or {}).get("jobPostingInfo", {}) or {}
            if info:
                description = html_to_text(info.get("jobDescription", ""))
                posted_date = to_iso_date(info.get("startDate", ""))
                location = info.get("location", "") or location
                apply_url = info.get("externalUrl", "") or info.get(
                    "applyUrl", apply_url
                ) or apply_url
                job_id = info.get("jobReqId", job_id) or job_id

        return Job(
            company_name=company_name,
            title=title,
            location=location,
            job_id=job_id,
            apply_url=apply_url,
            posting_url=posting_url,
            posted_date=posted_date,
            description=truncate(description, 8000),
            source=self.name,
        )


CLIENT = WorkdayClient
