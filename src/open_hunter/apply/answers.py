"""Draft answers to application questions, grounded in the real resume + JD.

Hard rule: answer ONLY from the candidate's actual resume/profile and the job
description. Never invent employers, degrees, metrics, or experience. If a
question can't be answered truthfully from the provided facts, return an empty
string so the human can fill it in during review.
"""

from __future__ import annotations

import json
from typing import Optional

from ..llm import LLMClient, LLMError
from ..logging_util import get_logger
from ..utils import truncate
from .profile import ApplicantProfile

log = get_logger(__name__)

_SYSTEM = (
    "You are helping a real job candidate draft answers to questions on a job "
    "application. Write in the candidate's first-person voice: concise, "
    "specific, professional, no clichés or fluff.\n\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY facts present in the candidate's profile/resume and the job "
    "description provided. NEVER invent employers, titles, dates, degrees, "
    "metrics, or projects.\n"
    "- If the question asks for something not supported by the provided facts "
    "(e.g. a salary expectation, a fact not in the resume), return an empty "
    "string \"\" so a human can complete it.\n"
    "- Tailor 'why this company/role' answers to the actual job description and "
    "the candidate's real skills/projects.\n"
    "- Keep length appropriate: 2-5 sentences for short prompts; up to ~150 "
    "words for cover-letter-style prompts unless the question says otherwise.\n"
    "Respond ONLY with JSON: {\"answer\": \"...\"}."
)


class AnswerEngine:
    def __init__(self, llm: Optional[LLMClient], profile: ApplicantProfile):
        self.llm = llm
        self.profile = profile

    def answer(self, question: str, job: dict, max_words: int = 0) -> str:
        """Draft a free-text answer. Returns '' if it can't be grounded."""
        if self.llm is None or not question.strip():
            return ""

        facts = {
            "name": self.profile.full_name,
            "skills": self.profile.resume.get("skills", []),
            "experience_summary": self.profile.resume.get("experience_summary", ""),
            "projects": self.profile.resume.get("projects", []),
            "education": self.profile.resume.get("education", ""),
            "resume_text": truncate(self.profile.resume_text, 4000),
        }
        ctx = {
            "question": question.strip(),
            "max_words": max_words or "use your judgement",
            "job": {
                "company": job.get("company_name", ""),
                "title": job.get("title", ""),
                "description": truncate(job.get("description", ""), 2500),
            },
            "candidate": facts,
        }
        try:
            out = self.llm.complete_json(
                _SYSTEM, json.dumps(ctx, ensure_ascii=False)
            )
            ans = str((out or {}).get("answer", "")).strip()
            return ans
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.debug("answer drafting failed for %r: %s", question[:60], exc)
            return ""

    def pick_option(self, question: str, desired: str,
                    options: list) -> str:
        """Choose the best dropdown option (flexible, not exact-match).

        Given the question, the candidate's intended answer, and the actual
        option list, the LLM returns the option text that best fits — handling
        wording differences (e.g. 'Decline To Self Identify' vs 'I prefer not
        to say', 'Bachelor's' vs 'Undergraduate degree', Yes/No variants).
        Returns '' if nothing fits.
        """
        opts = [o for o in (options or []) if o and o.strip()]
        if not opts or self.llm is None:
            return ""
        sysmsg = (
            "You select the single best option for a dropdown on a job "
            "application, given the candidate's intended answer. Match by "
            "meaning, not exact words. Respond ONLY with JSON "
            "{\"option\": \"<exact text copied from the options list, or empty "
            "string if none reasonably fits>\"}."
        )
        payload = {
            "question": question[:300],
            "candidate_intended_answer": desired,
            "options": opts[:60],
        }
        try:
            out = self.llm.complete_json(sysmsg, json.dumps(payload, ensure_ascii=False))
            choice = str((out or {}).get("option", "")).strip()
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.debug("pick_option failed for %r: %s", question[:50], exc)
            return ""
        if not choice:
            return ""
        # Ensure the choice is genuinely one of the options (exact, then loose).
        if choice in opts:
            return choice
        cl = choice.lower().strip()
        for o in opts:
            if o.lower().strip() == cl:
                return o
        for o in opts:
            if cl in o.lower() or o.lower() in cl:
                return o
        return ""
