"""Core data models shared across the pipeline.

Plain dataclasses keep things transparent: what you see is exactly what ends
up in the CSV. Every job we emit is a real, verified posting — there is no
field the pipeline is allowed to invent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional


@dataclass
class CompanyInput:
    """One row of the user's input file."""

    name: str
    website: Optional[str] = None  # optional hint supplied by the user
    notes: Optional[str] = None    # free-text the user attached, if any


@dataclass
class ResolvedCompany:
    """A company after we've figured out its real website + careers source."""

    name: str
    website: Optional[str] = None
    careers_url: Optional[str] = None
    # Which ATS we detected, e.g. "greenhouse", "lever", or "generic".
    source: Optional[str] = None
    # The ATS board token / tenant identifier, when applicable.
    source_token: Optional[str] = None
    # How confident we are in the website match (0..1), set by the resolver.
    resolution_confidence: float = 0.0
    # Human-readable reason, useful for the unresolved report.
    resolution_note: str = ""


@dataclass
class Job:
    """A single open role. These fields become CSV columns, in this order."""

    company_name: str
    title: str = ""
    location: str = ""
    job_id: str = ""
    apply_url: str = ""        # the link you click to apply
    posting_url: str = ""      # the public listing page (may equal apply_url)
    posted_date: str = ""      # ISO yyyy-mm-dd when known, else ""
    employment_type: str = ""  # full-time / contract / intern, when known
    department: str = ""
    skills: str = ""           # comma-separated, extracted from the description
    description: str = ""      # plain-text job description (cleaned)
    source: str = ""           # which extractor produced this (greenhouse, ...)

    # --- relevance, filled in by the matcher (not part of raw extraction) ---
    relevance: str = ""        # "match" | "maybe" | "no"
    relevance_score: float = 0.0
    relevance_reason: str = ""

    def dedupe_key(self) -> str:
        """Identity used to drop duplicates across sources."""
        if self.apply_url:
            return self.apply_url.strip().lower()
        return f"{self.company_name}|{self.title}|{self.location}".lower()

    def as_row(self) -> dict:
        return asdict(self)


# The CSV column order, exposed so the writer and tests agree on one source.
JOB_CSV_COLUMNS: List[str] = [
    "company_name",
    "title",
    "location",
    "job_id",
    "apply_url",
    "posting_url",
    "posted_date",
    "employment_type",
    "department",
    "skills",
    "relevance",
    "relevance_score",
    "relevance_reason",
    "description",
    "source",
]


@dataclass
class CompanyResult:
    """Everything we learned about one company in a single run."""

    company: ResolvedCompany
    jobs: List[Job] = field(default_factory=list)
    status: str = "pending"   # ok | no_jobs | unresolved | error | blocked
    error: str = ""
    finished_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds")
    )
