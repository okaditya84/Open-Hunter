"""Label-driven form filler that generalises across ATS platforms.

Rather than hard-coding selectors per ATS, we read every visible field, derive
its human label, classify it semantically, and fill it from the applicant's
real facts (or draft a grounded free-text answer). This degrades gracefully on
unfamiliar forms and is recorded field-by-field for the review step.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..logging_util import get_logger
from .answers import AnswerEngine
from .profile import ApplicantProfile

log = get_logger(__name__)


# ---- field classification -------------------------------------------------- #
def _kw(*words):
    return re.compile(r"|".join(words), re.I)


_CLASSIFIERS = [
    ("first_name", _kw(r"first[\s_]*name", r"given name")),
    ("last_name", _kw(r"last[\s_]*name", r"family name", r"surname")),
    ("full_name", _kw(r"\bfull name\b", r"^name$", r"your name", r"\bname\*?$")),
    ("email", _kw(r"e-?mail")),
    ("phone", _kw(r"phone", r"mobile", r"contact number")),
    ("linkedin", _kw(r"linkedin")),
    ("github", _kw(r"github", r"git hub")),
    ("portfolio", _kw(r"portfolio", r"website", r"personal site", r"\burl\b")),
    ("location", _kw(r"location", r"where are you", r"current city", r"address")),
    ("city", _kw(r"\bcity\b")),
    ("country", _kw(r"\bcountry\b")),
    ("current_company", _kw(r"current (company|employer)", r"present company",
                            r"\bcompany\b", r"\bemployer\b")),
    ("current_title", _kw(r"current (title|role|position)", r"job title")),
    ("school", _kw(r"school", r"university", r"college", r"institution")),
    ("grad_month", _kw(r"graduation month", r"grad(uation)? month")),
    ("grad_year", _kw(r"graduation year", r"grad(uation)? year",
                      r"year of passing", r"graduation date", r"graduation")),
    ("field_of_study", _kw(r"field of study", r"\bmajor\b", r"specialization",
                           r"discipline", r"area of study")),
    ("degree", _kw(r"\bdegree\b", r"qualification", r"education level",
                   r"highest education")),
    ("salary", _kw(r"salary", r"compensation", r"\bctc\b", r"expected pay")),
    ("notice", _kw(r"notice period", r"start date", r"availab", r"when can you")),
    ("sponsorship", _kw(r"sponsor", r"visa")),
    ("work_auth", _kw(r"authoriz", r"authoris", r"legally", r"right to work",
                      r"eligible to work", r"work permit")),
    ("relocate", _kw(r"relocat")),
    ("gender", _kw(r"gender", r"\bsex\b")),
    ("race", _kw(r"race", r"ethnic")),
    ("veteran", _kw(r"veteran")),
    ("disability", _kw(r"disab")),
    ("pronouns", _kw(r"pronoun")),
    ("hispanic", _kw(r"hispanic", r"latino")),
    ("how_heard", _kw(r"how (did|do) you hear", r"how you heard",
                      r"hear about", r"how were you referred", r"referral source")),
    ("age18", _kw(r"18 years", r"over 18", r"at least 18", r"of legal age")),
    ("cover_letter", _kw(r"cover letter")),
    ("why", _kw(r"why (do you )?(want|are you|this|interested)", r"motivat",
                r"interest you")),
]


def classify(label: str, name: str = "", placeholder: str = "") -> str:
    blob = " ".join([label or "", name or "", placeholder or ""]).strip()
    if not blob:
        return ""
    for cat, rx in _CLASSIFIERS:
        if rx.search(blob):
            return cat
    return ""


# ---- value resolution ------------------------------------------------------ #
class ValueResolver:
    def __init__(self, profile: ApplicantProfile, answers: AnswerEngine, job: dict):
        self.p = profile
        self.ans = answers
        self.job = job

    def needs_sponsorship(self) -> Optional[bool]:
        wa = self.p.answers.get("work_authorization", {}) or {}
        v = wa.get("needs_sponsorship_outside_india")
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("yes", "true", "y", "1"):
                return True
            if s in ("no", "false", "n", "0"):
                return False
        return None  # unknown -> leave for human

    def authorized_here(self) -> Optional[bool]:
        # We can only be sure about India from the stated facts.
        loc = (self.job.get("location") or "").lower()
        wa = self.p.answers.get("work_authorization", {}) or {}
        if "india" in loc:
            return bool(wa.get("authorized_in_india", True))
        return None  # outside India -> depends on sponsorship; leave for human

    def text_value(self, cat: str, label: str) -> Optional[str]:
        p = self.p
        common = p.answers.get("common_answers", {}) or {}
        mapping = {
            "first_name": p.first_name,
            "last_name": p.last_name,
            "full_name": p.full_name,
            "email": p.email,
            "phone": p.phone,
            "linkedin": p.link("linkedin"),
            "github": p.link("github"),
            "portfolio": p.link("portfolio"),
            "location": p.get("location"),
            "city": p.get("city"),
            "country": p.get("country"),
            "current_company": p.get("current_company"),
            "current_title": p.get("current_title"),
            "school": p.get("school"),
            "degree": p.get("degree"),
            "field_of_study": p.get("field_of_study"),
            "grad_year": p.get("grad_year"),
            "salary": common.get("expected_salary", ""),
            "notice": common.get("notice_period", ""),
            "how_heard": common.get("how_did_you_hear", ""),
        }
        if cat in mapping:
            return mapping[cat] or None
        # Free-text / essay questions -> draft a grounded answer.
        if cat in ("cover_letter", "why") or _looks_like_essay(label):
            return self.ans.answer(label, self.job) or None
        return None

    def choice_target(self, cat: str) -> Optional[str]:
        """Preferred option *text* for a dropdown/combobox, without options."""
        eeo = self.p.answers.get("eeo", {}) or {}
        if cat == "gender":
            return eeo.get("gender") or "Decline To Self Identify"
        if cat == "race":
            return eeo.get("race_ethnicity") or "Decline To Self Identify"
        if cat == "hispanic":
            return "Not Hispanic or Latino"
        if cat == "veteran":
            return eeo.get("veteran_status") or "I am not a protected veteran"
        if cat == "disability":
            return eeo.get("disability_status") or "I do not want to answer"
        if cat in ("relocate", "age18"):
            return "Yes"
        if cat == "work_auth":
            return "Yes" if self.authorized_here() is True else None
        if cat == "sponsorship":
            loc = (self.job.get("location") or "").lower()
            need = self.needs_sponsorship()
            if "india" in loc and need is not None:
                return "Yes" if need else "No"
            return None  # outside India / unknown -> human decides
        return None

    def combo_desired(self, cat: str, label: str) -> Optional[str]:
        """What to type/select into a dropdown or autocomplete field."""
        return self.text_value(cat, label) or self.choice_target(cat)

    def choice_value(self, cat: str, label: str, options: List[str]) -> Optional[str]:
        """Pick the best option for a dropdown/radio, or None to skip."""
        eeo = self.p.answers.get("eeo", {}) or {}
        common = self.p.answers.get("common_answers", {}) or {}

        def pick(*prefs):
            return _best_option(options, [x for x in prefs if x])

        if cat == "gender":
            return pick(eeo.get("gender"), "Decline", "Prefer not")
        if cat == "race":
            return pick(eeo.get("race_ethnicity"), "Decline", "Prefer not")
        if cat == "hispanic":
            return pick("not hispanic", "no", "Decline")
        if cat == "veteran":
            return pick(eeo.get("veteran_status"), "not a protected veteran",
                        "not a veteran", "Decline")
        if cat == "disability":
            return pick(eeo.get("disability_status"), "do not want to answer",
                        "no, i don", "Decline")
        if cat == "relocate":
            return pick("yes")
        if cat == "age18":
            return pick("yes")
        if cat == "work_auth":
            auth = self.authorized_here()
            if auth is True:
                return pick("yes")
            return None  # uncertain -> leave for human review
        if cat == "sponsorship":
            need = self.needs_sponsorship()
            loc = (self.job.get("location") or "").lower()
            if "india" in loc and need is not None:
                return pick("no") if not need else pick("yes")
            return None  # outside India / unknown -> human decides
        if cat == "how_heard":
            return pick(common.get("how_did_you_hear"), "company", "other")
        return None


_ESSAY_HINT = _kw(r"why", r"describe", r"tell us", r"what (is|are|makes)",
                  r"best project", r"proud", r"motivat", r"cover letter",
                  r"interest", r"about you", r"experience with", r"how would")


def _looks_like_essay(label: str) -> bool:
    return bool(label and len(label) > 12 and _ESSAY_HINT.search(label))


def _best_option(options: List[str], prefs: List[str]) -> Optional[str]:
    low = [(o, o.lower()) for o in options if o and o.strip()]
    for pref in prefs:
        pl = pref.lower()
        for orig, o in low:
            if pl in o or o in pl:
                return orig
    return None
