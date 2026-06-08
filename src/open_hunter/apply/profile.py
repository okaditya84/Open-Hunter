"""Load and merge the applicant profile used to fill forms.

Two sources:
  * data/candidate_profile.json  — extracted from the resume (skills, projects,
    experience, education) by the profile loader.
  * data/apply_answers.json      — form-fillable facts not reliably in a resume
    (contact links, work authorization, EEO preferences, common answers).

Plus the raw resume text (for grounding free-text answers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import PROJECT_ROOT
from ..logging_util import get_logger

log = get_logger(__name__)


@dataclass
class ApplicantProfile:
    answers: Dict[str, Any] = field(default_factory=dict)   # apply_answers.json
    resume: Dict[str, Any] = field(default_factory=dict)    # candidate_profile.json
    resume_text: str = ""

    # ---- convenient accessors ----
    def get(self, key: str, default: str = "") -> str:
        return str(self.answers.get(key, default) or default)

    @property
    def first_name(self) -> str:
        return self.get("first_name") or self.full_name.split(" ")[0]

    @property
    def last_name(self) -> str:
        return self.get("last_name") or " ".join(self.full_name.split(" ")[1:])

    @property
    def full_name(self) -> str:
        return self.get("full_name") or self.resume.get("name", "")

    @property
    def email(self) -> str:
        return self.get("email")

    @property
    def phone(self) -> str:
        return self.get("phone")

    @property
    def resume_path(self) -> Optional[Path]:
        p = self.get("resume_path")
        if not p:
            return None
        path = (PROJECT_ROOT / p) if not Path(p).is_absolute() else Path(p)
        return path if path.exists() else None

    def link(self, kind: str) -> str:
        """Return a link (linkedin/github/portfolio), '' if unset/placeholder."""
        v = self.get(kind)
        if not v or v.upper().startswith("TODO"):
            return ""
        return v

    def unconfirmed_fields(self) -> list:
        """Fields still marked TODO that the user must confirm before submit."""
        todos = []
        for k, v in self.answers.items():
            if isinstance(v, str) and v.upper().startswith("TODO"):
                todos.append(k)
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, str) and v2.upper().startswith("TODO"):
                        todos.append(f"{k}.{k2}")
        if not self.resume_path:
            todos.append("resume_path (file not found)")
        return todos


def load_profile(
    answers_path: Optional[str] = None,
    profile_path: Optional[str] = None,
) -> ApplicantProfile:
    ap = ApplicantProfile()

    apath = Path(answers_path) if answers_path else PROJECT_ROOT / "data" / "apply_answers.json"
    if apath.exists():
        ap.answers = json.loads(apath.read_text(encoding="utf-8"))
    else:
        log.warning("apply_answers.json not found at %s", apath)

    ppath = Path(profile_path) if profile_path else PROJECT_ROOT / "data" / "candidate_profile.json"
    if ppath.exists():
        ap.resume = json.loads(ppath.read_text(encoding="utf-8"))

    # Raw resume text for grounding (prefer the polished resume txt).
    for cand in ("Resume_Aditya_Jethani.txt", "Aditya_Jethani.txt"):
        tp = PROJECT_ROOT / "data" / cand
        if tp.exists():
            ap.resume_text = tp.read_text(encoding="utf-8", errors="ignore")
            break
    if not ap.resume_text and ap.resume:
        # Fall back to a compact summary built from the structured profile.
        ap.resume_text = json.dumps(ap.resume, ensure_ascii=False)

    return ap
