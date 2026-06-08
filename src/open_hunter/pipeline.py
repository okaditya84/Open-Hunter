"""The orchestrator: company list in, scored jobs + report out.

Per company:
  resolve name -> website
      -> discover careers source (ATS link or careers pages)
          -> ATS API client  (preferred: accurate, structured)
             or generic LLM extractor (fallback for bespoke sites)
Then, across all companies: de-duplicate, score against the user's criteria,
and write the CSVs.

Companies are processed concurrently (network-bound work). The headless
browser is internally serialised onto its own thread, so concurrency is safe.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .ats.base import get_client
from .config import Config
from .crawler import BrowserCrawler
from .discovery import discover
from .generic_extract import GenericExtractor
from .http_client import BlockedError, HttpClient
from .llm import LLMClient, LLMError
from .logging_util import get_logger
from .matcher import Matcher
from .models import CompanyInput, CompanyResult, Job, ResolvedCompany
from .resolver import CompanyResolver

log = get_logger(__name__)


class Pipeline:
    def __init__(self, config: Config):
        self.config = config
        self.http = HttpClient(config)
        self.browser = BrowserCrawler(config)

        self.llm: Optional[LLMClient] = None
        if config.llm_configured:
            try:
                self.llm = LLMClient(config)
            except LLMError as exc:
                log.warning("LLM disabled: %s", exc)
        else:
            log.warning(
                "LLM not configured (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL). "
                "Resolution falls back to heuristics; generic extraction and "
                "relevance scoring are disabled."
            )

        self.resolver = CompanyResolver(config, self.http, self.llm)
        self.generic = GenericExtractor(
            config, self.http, self.browser, self.llm
        )

    # ------------------------------------------------------------------ #
    def run(
        self,
        companies: List[CompanyInput],
        criteria: str,
        profile: Optional[dict] = None,
        max_age_days: int = 0,
    ) -> List[CompanyResult]:
        results: List[CompanyResult] = []
        total = len(companies)
        log.info("processing %d companies (concurrency=%d)",
                 total, self.config.max_concurrency)

        with ThreadPoolExecutor(max_workers=self.config.max_concurrency) as pool:
            futures = {
                pool.submit(self._process_company, c): c for c in companies
            }
            done = 0
            for fut in as_completed(futures):
                company = futures[fut]
                done += 1
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    res = CompanyResult(
                        company=ResolvedCompany(name=company.name),
                        status="error",
                        error=str(exc),
                    )
                results.append(res)
                log.info(
                    "[%d/%d] %-30s %-11s jobs=%d",
                    done, total, company.name[:30], res.status, len(res.jobs)
                )

        # Global de-duplication across companies.
        all_jobs = self._dedupe([j for r in results for j in r.jobs])

        # Pre-filter by posting date BEFORE the (costly) LLM matching, so we
        # never pay to score stale jobs. Jobs without a date are kept.
        if max_age_days and max_age_days > 0:
            from datetime import date, timedelta

            cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
            before = len(all_jobs)
            kept = [
                j for j in all_jobs
                if (not j.posted_date) or j.posted_date >= cutoff
            ]
            # Drop the filtered-out jobs from each company's result too, so the
            # report and CSV reflect what was actually considered.
            keep_ids = {id(j) for j in kept}
            for r in results:
                r.jobs = [j for j in r.jobs if id(j) in keep_ids]
            log.info(
                "date filter (<= %d days, cutoff %s): kept %d of %d jobs",
                max_age_days, cutoff, len(kept), before,
            )
            all_jobs = kept

        # Score relevance (inclusive), batches scored concurrently.
        Matcher(
            self.llm, criteria, profile,
            concurrency=self.config.matcher_concurrency,
        ).score(all_jobs)

        return results

    # ------------------------------------------------------------------ #
    def _process_company(self, company: CompanyInput) -> CompanyResult:
        resolved = self.resolver.resolve(company)
        result = CompanyResult(company=resolved)

        if not resolved.website:
            result.status = "unresolved"
            result.error = resolved.resolution_note
            return result

        try:
            disc = discover(self.http, resolved)
        except BlockedError as exc:
            result.status = "blocked"
            result.error = str(exc)
            return result

        if disc.careers_urls:
            resolved.careers_url = disc.careers_urls[0]

        jobs: List[Job] = []

        if disc.ats_name and disc.ats_token:
            resolved.source = disc.ats_name
            resolved.source_token = disc.ats_token
            client = get_client(disc.ats_name, self.http)
            if client:
                try:
                    jobs = client.fetch_jobs(disc.ats_token, company.name)
                except BlockedError as exc:
                    result.status = "blocked"
                    result.error = str(exc)
                    return result
                except Exception as exc:  # noqa: BLE001
                    log.debug("ATS fetch failed for %s: %s", company.name, exc)
        else:
            resolved.source = "generic"
            jobs = self.generic.extract(company.name, disc.careers_urls)

        result.jobs = jobs
        if jobs:
            result.status = "ok"
        elif disc.blocked:
            result.status = "blocked"
            result.error = "career pages blocked automated access"
        elif not disc.careers_urls:
            result.status = "unresolved"
            result.error = "no careers page found"
        else:
            result.status = "no_jobs"
            result.error = "careers page found but no open roles extracted"
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedupe(jobs: List[Job]) -> List[Job]:
        seen = set()
        out = []
        for j in jobs:
            key = j.dedupe_key()
            if key in seen:
                continue
            seen.add(key)
            out.append(j)
        return out

    def close(self) -> None:
        self.browser.close()
        self.http.close()
