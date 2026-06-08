"""Filter jobs against the user's criteria using the LLM.

Philosophy (chosen by the user): be INCLUSIVE. When the model is unsure
whether a role matches, it labels it "maybe" and keeps it, so nothing
relevant is silently dropped. Clear non-matches are labelled "no".

The matcher also backfills the `skills` field from the description when the
source didn't provide one, so the CSV is as complete as possible.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from .llm import LLMClient, LLMError
from .logging_util import get_logger
from .models import Job
from .utils import truncate

log = get_logger(__name__)

_BATCH = 8

_SYSTEM = (
    "You match job postings to a candidate's search criteria. The criteria is "
    "free text written by the candidate; it may mention desired roles, skills, "
    "location, a maximum years-of-experience, and an education ceiling.\n\n"
    "For EACH job decide relevance:\n"
    "  'match' = clearly fits the criteria.\n"
    "  'maybe' = plausibly fits, or you are genuinely unsure. BE INCLUSIVE: "
    "when uncertain, prefer 'maybe' over 'no'.\n"
    "  'no'    = clearly unrelated to the desired role/skills, OR the job's "
    "MINIMUM hard requirements exceed a limit the criteria states.\n\n"
    "Judging hard requirements (apply ONLY when the criteria states such a "
    "limit):\n"
    "- EXPERIENCE: find the job's MINIMUM required years of experience. Ignore "
    "wording like 'preferred', 'a plus', 'nice to have', 'bonus'. If that "
    "minimum clearly exceeds the candidate's stated maximum (e.g. criteria "
    "allows up to 2 years but the job requires 5+), mark 'no'. Titles such as "
    "Senior/Sr/Staff/Principal/Lead/Manager/Director/Head normally imply "
    "experience beyond an entry-level candidate; mark 'no' unless the stated "
    "minimum experience is within the candidate's range.\n"
    "- EDUCATION: find the job's MINIMUM required degree, NOT the preferred "
    "one. A job that requires a Bachelor's degree is suitable for a "
    "Bachelor's-ceiling candidate EVEN IF it also lists a Master's or PhD as "
    "preferred or 'or equivalent'. Only mark 'no' on education when the job's "
    "MINIMUM acceptable degree is strictly higher than the candidate's ceiling "
    "(e.g. the posting requires a Master's or PhD and offers no Bachelor's "
    "option).\n"
    "- If the criteria does not mention an experience or education limit, do "
    "NOT filter on it.\n\n"
    "Also extract up to ~10 key skills/technologies the posting mentions.\n"
    "Respond ONLY with JSON: {\"results\": [{\"index\": <int>, \"relevance\": "
    "\"match|maybe|no\", \"score\": 0.0-1.0, \"reason\": \"short\", "
    "\"skills\": \"comma-separated\"}]}. Include every index you were given."
)

_SYSTEM_WITH_PROFILE = (
    "You match job postings to a candidate's profile and criteria. For EACH job, decide "
    "relevance:\n"
    "  'match' = clearly fits the candidate's profile and criteria,\n"
    "  'maybe' = plausibly fits or you are uncertain (BE INCLUSIVE — when in "
    "doubt choose 'maybe', never 'no'),\n"
    "  'no'    = clearly unrelated, OR does not meet experience/seniority requirements.\n"
    "\n"
    "CRITICAL EXPERIENCE & SENIORITY RULES:\n"
    "1. Check the candidate's experience level/restrictions in the profile (e.g. 0-2 years, no senior/lead/staff roles).\n"
    "2. If the job title or description mentions Senior, Lead, Staff, Principal, Manager, Director, "
    "or requires years of experience exceeding the candidate's experience limit (e.g. requiring 3+, 5+, 7+ years of experience), "
    "you MUST mark it 'no'. Do NOT mark it 'maybe'. This is a hard filter.\n"
    "3. If the job is entry-level, junior, associate, fresher, or has 0-2 years experience requirement (or no experience requirement is stated but it doesn't look senior), it matches the experience criteria.\n"
    "\n"
    "Also extract up to ~10 key skills/technologies the posting mentions.\n"
    "Respond ONLY with JSON: {\"results\": [{\"index\": <int>, \"relevance\": "
    "\"match|maybe|no\", \"score\": 0.0-1.0, \"reason\": \"short\", "
    "\"skills\": \"comma-separated\"}]}. Include every index you were given."
)


class Matcher:
    def __init__(self, llm: Optional[LLMClient], criteria: str,
                 profile: Optional[Dict[str, Any]] = None,
                 concurrency: int = 8):
        self.llm = llm
        self.criteria = (criteria or "").strip()
        self.profile = profile
        self.concurrency = max(1, concurrency)

    def score(self, jobs: List[Job]) -> List[Job]:
        if not jobs:
            return jobs

        # No criteria and no profile => keep everything, no filtering.
        if not self.criteria and not self.profile:
            for j in jobs:
                j.relevance = "match"
                j.relevance_score = 1.0
                j.relevance_reason = "no criteria or profile supplied; included by default"
            return jobs

        # No LLM => we can't judge; keep everything honestly labelled.
        if self.llm is None:
            for j in jobs:
                j.relevance = "unknown"
                j.relevance_reason = "no LLM configured to assess relevance"
            return jobs

        # Pre-filter senior/management roles if candidate profile indicates entry-level
        jobs_to_score = []
        is_entry_level = False
        if self.profile:
            exp_years = self.profile.get("experience_years", 0.0)
            restriction = str(self.profile.get("experience_level_restriction", "")).lower()
            if exp_years <= 3.0 or any(k in restriction for k in ["entry", "fresher", "junior", "0-2", "0-3", "1-2", "intern", "graduate"]):
                is_entry_level = True

        if is_entry_level:
            senior_pattern = re.compile(
                r"\b(senior|sr\b|lead|staff|principal|director|manager|head|vp|chief|architect)\b",
                re.IGNORECASE
            )
            for j in jobs:
                if senior_pattern.search(j.title):
                    j.relevance = "no"
                    j.relevance_score = 0.0
                    j.relevance_reason = "pre-filtered: title indicates senior or management role"
                else:
                    jobs_to_score.append(j)
        else:
            jobs_to_score = jobs

        batches = [
            jobs_to_score[s : s + _BATCH]
            for s in range(0, len(jobs_to_score), _BATCH)
        ]
        if not batches:
            return jobs

        # Score batches concurrently. Each batch mutates its own disjoint Job
        # objects, and the LLM client is thread-safe, so this is safe; it turns
        # a long serial wall of LLM calls into a parallel one.
        total = len(batches)
        workers = min(self.concurrency, total)
        log.info("scoring %d jobs in %d batches (concurrency=%d)",
                 len(jobs_to_score), total, workers)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._score_batch, b): b for b in batches}
            for fut in as_completed(futures):
                done += 1
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - keep going on a bad batch
                    log.debug("a scoring batch failed: %s", exc)
                    for j in futures[fut]:
                        if not j.relevance:
                            j.relevance = "maybe"
                            j.relevance_reason = "scoring batch errored; kept to be safe"
                if done % 10 == 0 or done == total:
                    log.info("  matched %d/%d batches", done, total)
        return jobs

    def _score_batch(self, batch: List[Job]) -> None:
        payload: Dict[str, Any] = {
            "criteria": self.criteria,
            "jobs": [
                {
                    "index": i,
                    "title": j.title,
                    "location": j.location,
                    "department": j.department,
                    # Wide enough to include the "Qualifications/Requirements"
                    # section, where minimum degree & experience usually live.
                    "description": truncate(j.description, 2000),
                }
                for i, j in enumerate(batch)
            ],
        }
        if self.profile:
            # Compress candidate profile to save tokens
            payload["candidate_profile"] = {
                "name": self.profile.get("name"),
                "experience_years": self.profile.get("experience_years"),
                "experience_summary": self.profile.get("experience_summary"),
                "target_roles": self.profile.get("target_roles"),
                "experience_level_restriction": self.profile.get("experience_level_restriction"),
                "skills": self.profile.get("skills", [])[:15],
            }

        system_prompt = _SYSTEM_WITH_PROFILE if self.profile else _SYSTEM
        try:
            result = self.llm.complete_json(
                system_prompt, json.dumps(payload, ensure_ascii=False)
            )
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.debug("matcher LLM call failed: %s", exc)
            # Fail safe to inclusive: keep as 'maybe' rather than drop.
            for j in batch:
                j.relevance = "maybe"
                j.relevance_reason = "relevance check failed; kept to be safe"
            return

        by_index = {}
        for item in (result or {}).get("results", []):
            try:
                by_index[int(item.get("index"))] = item
            except (TypeError, ValueError):
                continue

        for i, job in enumerate(batch):
            item = by_index.get(i)
            if not item:
                job.relevance = "maybe"
                job.relevance_reason = "not returned by matcher; kept to be safe"
                continue
            rel = str(item.get("relevance", "maybe")).lower()
            if rel not in {"match", "maybe", "no"}:
                rel = "maybe"
            job.relevance = rel
            try:
                job.relevance_score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                job.relevance_score = 0.0
            job.relevance_reason = str(item.get("reason", ""))[:300]
            skills = str(item.get("skills", "")).strip()
            if skills and not job.skills:
                job.skills = skills
