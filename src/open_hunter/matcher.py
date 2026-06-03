"""Filter jobs against the user's criteria using the LLM.

Philosophy (chosen by the user): be INCLUSIVE. When the model is unsure
whether a role matches, it labels it "maybe" and keeps it, so nothing
relevant is silently dropped. Clear non-matches are labelled "no".

The matcher also backfills the `skills` field from the description when the
source didn't provide one, so the CSV is as complete as possible.
"""

from __future__ import annotations

import json
from typing import List, Optional

from .llm import LLMClient, LLMError
from .logging_util import get_logger
from .models import Job
from .utils import truncate

log = get_logger(__name__)

_BATCH = 8

_SYSTEM = (
    "You match job postings to a candidate's criteria. For EACH job, decide "
    "relevance:\n"
    "  'match' = clearly fits the criteria,\n"
    "  'maybe' = plausibly fits or you are uncertain (BE INCLUSIVE — when in "
    "doubt choose 'maybe', never 'no'),\n"
    "  'no'    = clearly unrelated.\n"
    "Also extract up to ~10 key skills/technologies the posting mentions.\n"
    "Respond ONLY with JSON: {\"results\": [{\"index\": <int>, \"relevance\": "
    "\"match|maybe|no\", \"score\": 0.0-1.0, \"reason\": \"short\", "
    "\"skills\": \"comma-separated\"}]}. Include every index you were given."
)


class Matcher:
    def __init__(self, llm: Optional[LLMClient], criteria: str):
        self.llm = llm
        self.criteria = (criteria or "").strip()

    def score(self, jobs: List[Job]) -> List[Job]:
        if not jobs:
            return jobs

        # No criteria => keep everything, no filtering.
        if not self.criteria:
            for j in jobs:
                j.relevance = "match"
                j.relevance_score = 1.0
                j.relevance_reason = "no criteria supplied; included by default"
            return jobs

        # No LLM => we can't judge; keep everything honestly labelled.
        if self.llm is None:
            for j in jobs:
                j.relevance = "unknown"
                j.relevance_reason = "no LLM configured to assess relevance"
            return jobs

        for start in range(0, len(jobs), _BATCH):
            batch = jobs[start : start + _BATCH]
            self._score_batch(batch)
        return jobs

    def _score_batch(self, batch: List[Job]) -> None:
        payload = {
            "criteria": self.criteria,
            "jobs": [
                {
                    "index": i,
                    "title": j.title,
                    "location": j.location,
                    "department": j.department,
                    "description": truncate(j.description, 1500),
                }
                for i, j in enumerate(batch)
            ],
        }
        try:
            result = self.llm.complete_json(
                _SYSTEM, json.dumps(payload, ensure_ascii=False)
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
